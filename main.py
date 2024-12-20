"""
Inactivity Tracker Module

Copyright (C) 2024  __retr0.init__

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import asyncio
import os
from collections import namedtuple
from datetime import datetime
from typing import Generator, List, Optional

import interactions
import sqlalchemy
import sqlalchemy.dialects.sqlite as sqlite
from interactions import IntervalTrigger, Task
from interactions.api.events import MessageCreate
from sqlalchemy import delete as sqldelete
from sqlalchemy import select as sqlselect
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from src import logutil

from .model import (
    ConfigDB,
    ConfigRoles,
    DBBase,
    StrippedRole,
    StrippedRoles,
    StrippedUserDB,
    UserTimeDB,
)

logger = logutil.init_logger(os.path.basename(__file__))

"""
Sqlite3 DB async engine
"""
g_engine: AsyncEngine = create_async_engine(
    f"sqlite+aiosqlite:///{os.path.dirname(__file__)}/inactivity_mon_db.db"
)

"""
Sqlalchemy async session factory
"""
g_Session = async_sessionmaker(g_engine)


@sqlalchemy.event.listens_for(g_engine.sync_engine, "connect")
def do_connect(dbapi_connection, connection_record):
    dbapi_connection.isolation_level = None


@sqlalchemy.event.listens_for(g_engine.sync_engine, "begin")
def do_begin(conn):
    conn.exec_driver_sql("BEGIN")


"""
Sqlalchemy async session factory with proper connection management
"""
g_session: Optional[sqlalchemy.orm.Session] = None


async def get_session() -> sqlalchemy.orm.Session:
    """Get or create global session"""
    global g_session
    if g_session:
        return g_session()
    g_session = g_Session
    return g_session()


async def close_session():
    """Close and clear the global session"""
    global g_session
    if g_session:
        await g_session.close()
        g_session = None


async def commit_session():
    """Commit changes in the global session"""
    global g_session
    if g_session:
        await g_session.commit()


"""
The judge pause period in hour.
It is like updating the latest time that a user's message every `JUDGING_HOUR` hours.
It also adds to the judgeing time to temporarily remove roles from the user.
"""
JUDGING_HOUR: int = 24

"""
DB is updated with new values. Need to commit the changes in periodic task
"""
g_DB_updated: bool = False

"""
Whether the DB is initialising. It's to judge whether the initialising is in process
"""
g_data_initialising: bool = False

"""
Whether the DB is initialised. It's to judge whether all info is aquired for judging and execution
"""
h_data_initialised: asyncio.Event = asyncio.Event()

"""
A global flag on whether the execution process is started or not
"""
g_execution_started: bool = False

"""
Currently running role remove tasks. {user_id: task}
"""
g_running_tasks: dict[int, asyncio.Task] = {}


class ChannelHistoryIteractor:
    def __init__(self, history: interactions.ChannelHistory):
        self.history: interactions.ChannelHistory = history

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.history.__anext__()
        except StopAsyncIteration:
            raise StopAsyncIteration
        except interactions.errors.HTTPException as e:
            try:
                match int(e.code):
                    case 50083:
                        """Operation in archived thread"""
                        logger.error(
                            f"Channel {self.history.channel.name} ({self.history.channel.id}) is an archived thread"
                        )
                        raise StopAsyncIteration
                    case 10003:
                        """Unknown channel"""
                        logger.error("Operating in an unknown channel")
                        raise StopAsyncIteration
                    case 10008:
                        """Unknown message"""
                        logger.warning(
                            f"Unknown message in Channel {self.history.channel.name} ({self.history.channel.id})"
                        )
                        pass
                    case 50001:
                        """No Access"""
                        logger.error(
                            f"Bot has no access to Channel {self.history.channel.name} ({self.history.channel.id})"
                        )
                        raise StopAsyncIteration
                    case 50013:
                        """Lack permission"""
                        logger.error(
                            f"Channel {self.history.channel.name} ({self.history.channel.id}) lacks permission"
                        )
                        raise StopAsyncIteration
                    case 50021:
                        """Cannot execute on system message"""
                        logger.warning(
                            f"System message in Channel {self.history.channel.name} ({self.history.channel.id})"
                        )
                        pass
                    case 160005:
                        """Thread is locked"""
                        logger.warning(
                            f"Channel {self.history.channel.name} ({self.history.channel.id}) is a locked thread"
                        )
                        pass
                    case _:
                        """Default"""
                        logger.warning(
                            f"Channel {self.history.channel.name} ({self.history.channel.id}) has unknown code {e.code}"
                        )
                        pass
            except ValueError:
                logger.warning(
                    f"Unknown HTTP exception {e.code} {e.errors} {e.route} {e.response} {e.text}"
                )
                pass
        except Exception as e:
            logger.warning(
                f"Unknown exception {e.code} {e.errors} {e.route} {e.response} {e.text}"
            )
            pass


# async def search_latest_msg_ch(user_id: int, channel: interactions.MessageableMixin) -> Optional[interactions.Message]:
#     """
#     Find the latest message of a user in a channel. If no message is found, return None
#     """
#     result: Optional[interactions.Message] = None
#     history: interactions.ChannelHistory = channel.history(0)
#     async for msg in ChannelHistoryIteractor(history=history):
#         if msg.author.id == user_id:
#             result = msg
#             break
#     return result

UserTime = namedtuple("UserTime", "user time index")

Mem_UserTimes: list[UserTime] = []


async def fetch_list_user_latest_msg_ch(
    channel: interactions.MessageableMixin,
) -> list[UserTime]:
    """
    Get the list of (user, time) for the latest message in a channel
    """
    result: list[UserTime] = []
    history: interactions.ChannelHistory = channel.history(0)
    async for msg in ChannelHistoryIteractor(history=history):
        if msg.author.id not in (r.user for r in result):
            tt = msg.edited_timestamp if msg.edited_timestamp else msg.timestamp
            result.append(UserTime(msg.author.id, tt.timestamp(), len(result)))
    return result


def merge_list_usertime_latest(usertimess: list[list[UserTime]]) -> list[UserTime]:
    """
    Merge lists of list of usertime with the latest time
    """
    result: list[UserTime] = []
    found: bool = False
    for usertimes in usertimess:
        for ut in usertimes:
            found = False
            for i, res_ut in enumerate(result):
                if ut.user == res_ut.user:
                    found = True
                    if ut.time < res_ut.time:
                        result[i] = UserTime(res_ut.user, res_ut.time, i)
                    break
            if not found:
                result.append(UserTime(ut.user, ut.time, len(result)))
    return result


def filter_usertime_time(
    usertimes: list[UserTime], timestamp: float
) -> Generator[UserTime]:
    """
    Filter usertime list before (less than) given timestamp. It skips the JUDGING_HOUR for filtering.
    """
    result: Generator[UserTime] = (
        usertime
        for usertime in usertimes
        if usertime.time < (timestamp - JUDGING_HOUR * 60 * 60)
    )
    return result


async def upsert_db_usertime(ut: UserTime) -> None:
    """
    Update or insert a user's latest message timestamp in the database.
    Only executes the operation without committing.
    """
    try:
        session = await get_session()
        existing = await session.execute(
            sqlselect(UserTimeDB).where(UserTimeDB.user == ut.user)
        )
        user_time = existing.scalar_one_or_none()

        if user_time:
            user_time.timestamp = ut.time
        else:
            session.add(UserTimeDB(user=ut.user, timestamp=ut.time))

        global g_DB_updated
        g_DB_updated = True

    except Exception as e:
        logger.error(f"Failed to upsert user time for user {ut.user}: {e}")
        try:
            await session.rollback()
        except Exception as rollback_error:
            logger.error(f"Failed to rollback user time upsert: {rollback_error}")


async def execute_member(member: interactions.Member) -> None:
    """Execute inactivity check and role management for a single member."""
    extension = member._client.get_ext("Retr0InitInactivityTrack")
    if not extension:
        logger.error("Could not find Retr0InitInactivityTrack extension")
        return

    try:
        session = await get_session()
        result = await session.execute(
            sqlselect(UserTimeDB).where(UserTimeDB.user == member.id)
        )
        user_time = result.scalar_one_or_none()

        if not user_time:
            logger.warning(
                f"No activity record found for member {member.display_name} ({member.id})"
            )
            return

        current_time = datetime.now().timestamp()
        time_since_last_activity = current_time - user_time.timestamp

        if time_since_last_activity >= JUDGING_HOUR * 3600:
            roles_to_store = [
                StrippedRole(id=role.id, name=role.name)
                for role in member.roles
                if role.id != member.guild.id
            ]
            stripped_roles = StrippedRoles(roles=roles_to_store)

            existing = await session.execute(
                sqlselect(StrippedUserDB).where(StrippedUserDB.user == member.id)
            )
            user_roles = existing.scalar_one_or_none()

            if user_roles:
                user_roles.roles = stripped_roles.model_dump_json()
            else:
                session.add(
                    StrippedUserDB(
                        user=member.id, roles=stripped_roles.model_dump_json()
                    )
                )

            global g_DB_updated
            g_DB_updated = True

            logger.info(
                f"Stored roles for member {member.display_name} ({member.id}): {stripped_roles.model_dump_json()}"
            )

            roles_to_remove = [role.id for role in stripped_roles.roles]
            await member.remove_roles(
                roles_to_remove, reason="Inactivity after long period of time"
            )

            if extension.role_id_assign != 0:
                await member.add_role(extension.role_id_assign)

            logger.info(
                f"Executed inactivity actions for member {member.display_name} ({member.id})"
            )

    except Exception as e:
        logger.error(
            f"Failed to execute member {member.display_name} ({member.id}): {e}"
        )
        try:
            await session.rollback()
        except Exception as rollback_error:
            logger.error(f"Failed to rollback member execution: {rollback_error}")


class Retr0InitInactivityTrack(interactions.Extension):
    module_base: interactions.SlashCommand = interactions.SlashCommand(
        name="inactivity",
        description="Inactivity tracking module",
        default_member_permissions=interactions.Permissions.ADMINISTRATOR,
    )
    module_group_setting: interactions.SlashCommand = module_base.group(
        name="setting", description="Configure the inactivity tracker module"
    )

    lock_db: asyncio.Lock = asyncio.Lock()

    info_gathering: bool = False
    info_gathered: bool = False
    started: bool = False
    role_id_assign: int = 0
    ignored_roles: List[int] = []
    specific_roles: List[int] = []
    execution_time_second: int = 86400

    async def async_start(self) -> None:
        """Load all data from database only when the bot starts up"""
        await self.init_data()
        self.task_db_commit.start()

    def drop(self) -> None:
        """Do not modify"""
        asyncio.create_task(self.async_drop())
        super().drop()

    async def async_drop(self) -> None:
        """Cleanup after the extension is unloaded"""
        try:
            if hasattr(self, "task_db_commit"):
                self.task_db_commit.stop()

            await self.func_task_db_commit()

            for task in g_running_tasks.values():
                if not task.done():
                    task.cancel()

            if g_running_tasks:
                await asyncio.gather(*g_running_tasks.values(), return_exceptions=True)
            g_running_tasks.clear()

            await close_session()

            await g_engine.dispose()
            logger.info("Database connection closed")

        except Exception as e:
            logger.error(f"Error during extension cleanup: {e}")

    async def execute_member_after_task(self, user_id: int, seconds: int) -> None:
        try:
            await asyncio.sleep(seconds)
            if member := await self.bot.guilds[0].fetch_member(user_id):
                if len(self.specific_roles) > 0 and all(
                    sr not in member._role_ids for sr in self.specific_roles
                ):
                    return
                if any(ir in member._role_ids for ir in self.ignored_roles):
                    return
                if self.role_id_assign not in member._role_ids:
                    roles_to_remove = [
                        role
                        for role in member.roles
                        if role.id != member.guild.id
                        and role.id not in self.ignored_roles
                        and (not self.specific_roles or role.id in self.specific_roles)
                    ]

                    stripped_roles = StrippedRoles(
                        roles=[
                            StrippedRole(id=role.id, name=role.name)
                            for role in roles_to_remove
                        ]
                    )

                    session = await get_session()
                    existing = await session.execute(
                        sqlselect(StrippedUserDB).where(StrippedUserDB.user == user_id)
                    )
                    user_roles = existing.scalar_one_or_none()

                    if user_roles:
                        user_roles.roles = stripped_roles.model_dump_json()
                    else:
                        session.add(
                            StrippedUserDB(
                                user=user_id, roles=stripped_roles.model_dump_json()
                            )
                        )

                    global g_DB_updated
                    g_DB_updated = True
                    logger.info(
                        f"Stored roles for user {user_id}: {stripped_roles.model_dump_json()}"
                    )

                    await member.remove_roles(
                        roles_to_remove, "Inactivity after long period of time"
                    )
                    if self.role_id_assign != 0:
                        await member.add_role(self.role_id_assign)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in execute_member_after_task for user {user_id}: {e}")

    def upsert_emat_task(self, user_id: int, seconds: int) -> None:
        """Update or create an execution task for a user"""
        try:
            existing_task = g_running_tasks.get(user_id)
            if existing_task and not existing_task.done():
                existing_task.cancel()

            task = asyncio.create_task(
                self.execute_member_after_task(user_id, seconds),
                name=f"inactivity_check_{user_id}",
            )

            g_running_tasks[user_id] = task

            def cleanup_task(future):
                if user_id in g_running_tasks:
                    del g_running_tasks[user_id]

            task.add_done_callback(cleanup_task)

        except Exception as e:
            logger.error(f"Failed to create/update task for user {user_id}: {e}")

    @module_base.subcommand(
        "status", sub_cmd_description="Get current status and summary"
    )
    async def module_base_status(self, ctx: interactions.SlashContext):
        """Display current status and summary of the inactivity tracker"""
        try:
            status_lines = [
                "**Inactivity Tracker Status**",
                f"- Initialization: {'Complete' if h_data_initialised.is_set() else 'Pending'}",
                f"- Execution Process: {'Running' if g_execution_started else 'Stopped'}",
                f"- Active Tasks: {len(g_running_tasks)}",
                f"- Tracked Users: {len(Mem_UserTimes)}",
            ]

            current_time = datetime.now().timestamp()
            upcoming_executions = []
            for user_id, task in g_running_tasks.items():
                if not task.done():
                    user_time = next(
                        (ut for ut in Mem_UserTimes if ut.user == user_id), None
                    )
                    if user_time:
                        time_until_execution = (
                            user_time.time + self.execution_time_second
                        ) - current_time
                        hours_remaining = time_until_execution / 3600
                        if hours_remaining > 0:
                            upcoming_executions.append((user_id, hours_remaining))

            if upcoming_executions:
                status_lines.append("\n**Next Executions:**")
                upcoming_executions.sort(key=lambda x: x[1])
                for user_id, hours in upcoming_executions[:5]:
                    status_lines.append(
                        f"- User {user_id}: {hours:.1f} hours remaining"
                    )

            status_lines.extend(
                [
                    "\n**Configuration Summary:**",
                    f"- Inactivity Period: {self.execution_time_second // 3600} hours",
                    f"- Inactivity Role: {self.role_id_assign if self.role_id_assign != 0 else 'Disabled'}",
                    f"- Ignored Roles: {len(self.ignored_roles)}",
                    f"- Monitored Roles: {'All' if not self.specific_roles else len(self.specific_roles)}",
                ]
            )

            await ctx.send("\n".join(status_lines), ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to get status: {e}")
            await ctx.send("Failed to retrieve status information", ephemeral=True)

    async def init_data(self) -> None:
        """Initialize and prepare data for this module to operate"""
        global h_data_initialised, g_data_initialising
        if h_data_initialised.is_set():
            return
        g_data_initialising = True

        try:
            async with g_engine.begin() as conn:
                await conn.run_sync(DBBase.metadata.create_all)

            session = await get_session()
            config_query = await session.execute(sqlselect(ConfigDB))
            configs = config_query.scalars().all()

            for config in configs:
                match config.name:
                    case "role_id_assign":
                        self.role_id_assign = int(config.value)
                    case "ignored_roles":
                        roles = ConfigRoles.model_validate_json(config.value)
                        self.ignored_roles = roles.roles
                    case "specific_roles":
                        roles = ConfigRoles.model_validate_json(config.value)
                        self.specific_roles = roles.roles
                    case "execution_time":
                        self.execution_time_second = int(config.value)

            user_times_query = await session.execute(sqlselect(UserTimeDB))
            user_times = user_times_query.scalars().all()

            global Mem_UserTimes
            Mem_UserTimes = [
                UserTime(ut.user, ut.timestamp, idx)
                for idx, ut in enumerate(user_times)
            ]

            current_time = datetime.now().timestamp()
            inactive_users = [
                ut.user
                for ut in Mem_UserTimes
                if current_time - ut.time >= self.execution_time_second
            ]

            if inactive_users and self.bot.guilds:
                guild = self.bot.guilds[0]
                logger.info(
                    f"Found {len(inactive_users)} inactive users during startup"
                )

                for user_id in inactive_users:
                    try:
                        if member := await guild.fetch_member(user_id):
                            await execute_member(member)
                            logger.info(
                                f"Processed inactive member {member.display_name} ({member.id}) during startup"
                            )
                    except Exception as e:
                        logger.error(
                            f"Failed to process inactive member {user_id} during startup: {e}"
                        )

            logger.info("Startup initialization complete")

        except Exception as e:
            logger.error(f"Failed to initialize data: {e}")
            try:
                await session.rollback()
            except Exception as rollback_error:
                logger.error(f"Failed to rollback initialization: {rollback_error}")
            raise
        finally:
            h_data_initialised.set()
            g_data_initialising = False

    @module_base.subcommand(
        "init", sub_cmd_description="Prepare the data. Essential before start."
    )
    async def module_base_init(
        self,
        ctx: interactions.SlashContext,
    ) -> None:
        global h_data_initialised
        if h_data_initialised.is_set():
            await ctx.send("The data is already initialised!", ephemeral=True)
            return
        if g_data_initialising:
            await ctx.send("The data is being initalised!", ephemeral=True)
            return
        await self.init_data()
        pass

    @module_base.subcommand(
        "start", sub_cmd_description="Start the member execution only after initialised"
    )
    @interactions.slash_option(
        name="init_first",
        description="Initiliase the data before start.",
        required=False,
        opt_type=interactions.OptionType.BOOLEAN,
    )
    @interactions.slash_option(
        name="wait",
        description="Wait until data is initialised",
        required=False,
        opt_type=interactions.OptionType.BOOLEAN,
    )
    async def module_base_start(
        self,
        ctx: interactions.SlashContext,
        init_first: bool = False,
        wait: bool = False,
    ) -> None:
        global g_execution_started
        if g_execution_started:
            await ctx.send("The execution process is already started.", ephemeral=True)
            return
        if init_first and not h_data_initialised.is_set():
            await ctx.send("Data is being initliased. Please wait...", ephemeral=True)
            if not g_data_initialising:
                await self.init_data()
            else:
                await h_data_initialised.wait()
        if not init_first and not h_data_initialised.is_set():
            if not g_data_initialising:
                await ctx.send("The data is not initialised!", ephemeral=True)
                return
            elif wait:
                await ctx.send(
                    "The data is being initialised. Please wait...", ephemeral=True
                )
                await h_data_initialised.wait()
            elif not wait:
                await ctx.send(
                    "The data is being initialised! Do it later or set `wait` to `True`.",
                    ephemeral=True,
                )
                return
        self.task_db_commit.start()

        for ut in Mem_UserTimes:
            current_time = datetime.now().timestamp()
            time_since_last = current_time - ut.time
            remaining_time = max(0, self.execution_time_second - time_since_last)

            if remaining_time > 0:
                self.upsert_emat_task(ut.user, remaining_time)

        g_execution_started = True
        await ctx.send("Inactivity tracking started successfully!", ephemeral=True)
        pass

    @interactions.listen(MessageCreate)
    async def on_messagecreate(self, event: MessageCreate) -> None:
        """Event listener for new messages"""
        if not event.message.author or event.message.author.bot:
            return

        if not event.message.guild:
            return

        try:
            print(
                f"User {event.message.author.display_name} sent '{event.message.content}'"
            )

            try:
                session = await get_session()
                stored_roles = await session.execute(
                    sqlselect(StrippedUserDB).where(
                        StrippedUserDB.user == event.message.author.id
                    )
                )
                user_roles = stored_roles.scalar_one_or_none()

                if user_roles:
                    stripped_roles = StrippedRoles.model_validate_json(user_roles.roles)

                    if member := await event.message.guild.fetch_member(
                        event.message.author.id
                    ):
                        guild_roles = {
                            role.id: role for role in event.message.guild.roles
                        }

                        if self.role_id_assign != 0:
                            try:
                                await member.remove_role(self.role_id_assign)
                            except Exception as e:
                                logger.warning(f"Failed to remove inactivity role: {e}")

                        roles_restored = []
                        roles_skipped = []
                        for role in stripped_roles.roles:
                            if role.id in guild_roles:
                                try:
                                    await member.add_role(role.id)
                                    roles_restored.append(role.name)
                                except Exception as e:
                                    logger.warning(
                                        f"Failed to restore role {role.name} ({role.id}): {e}"
                                    )
                                    roles_skipped.append(role.name)
                            else:
                                logger.warning(
                                    f"Role {role.name} ({role.id}) no longer exists in guild, skipping"
                                )
                                roles_skipped.append(role.name)

                        await session.execute(
                            sqldelete(StrippedUserDB).where(
                                StrippedUserDB.user == event.message.author.id
                            )
                        )
                        global g_DB_updated
                        g_DB_updated = True

            except Exception as e:
                logger.error(f"Failed to process message event: {e}")
                try:
                    await session.rollback()
                except Exception as rollback_error:
                    logger.error(
                        f"Failed to rollback message processing: {rollback_error}"
                    )

            ut: Optional[UserTime] = next(
                (x for x in Mem_UserTimes if x.user == event.message.id), None
            )
            if not ut:
                Mem_UserTimes.append(
                    UserTime(
                        event.message.author.id,
                        event.message.timestamp.timestamp(),
                        len(Mem_UserTimes),
                    )
                )
                await upsert_db_usertime(Mem_UserTimes[-1])
                self.upsert_emat_task(
                    event.message.author.id, self.execution_time_second
                )
                return

            if ut.time + JUDGING_HOUR * 3600 <= event.message.timestamp.timestamp():
                Mem_UserTimes[ut.index] = UserTime(
                    ut.user, event.message.timestamp.timestamp(), ut.index
                )
                await upsert_db_usertime(Mem_UserTimes[ut.index])
                if g_execution_started:
                    self.upsert_emat_task(
                        event.message.author.id, self.execution_time_second
                    )

        except Exception as e:
            logger.error(f"Error processing message event: {e}", exc_info=True)

    @Task.create(IntervalTrigger(minutes=10))
    async def task_db_commit(self) -> None:
        """Periodic task to commit database changes if any updates occurred"""
        await self.func_task_db_commit()

    async def func_task_db_commit(self) -> None:
        """Commit database changes if any updates occurred"""
        global g_DB_updated
        if not g_DB_updated:
            return

        try:
            session = await get_session()
            await session.commit()
            g_DB_updated = False
            await close_session()
            logger.debug("Successfully committed database changes")
        except Exception as e:
            logger.error(f"Failed to commit database changes: {e}")
            try:
                session = await get_session()
                await session.rollback()
            except Exception as rollback_error:
                logger.error(f"Failed to rollback database changes: {rollback_error}")

    @module_group_setting.subcommand(
        "set_role", sub_cmd_description="Set the role to be assigned to inactive users"
    )
    @interactions.slash_option(
        name="role",
        description="The role to assign (none to disable)",
        required=False,
        opt_type=interactions.OptionType.ROLE,
    )
    async def module_group_setting_role(
        self, ctx: interactions.SlashContext, role: Optional[interactions.Role] = None
    ) -> None:
        try:
            session = await get_session()
            existing = await session.execute(
                sqlselect(ConfigDB).where(ConfigDB.name == "role_id_assign")
            )
            config = existing.scalar_one_or_none()

            role_id = str(role.id) if role else "0"

            if config:
                config.value = role_id
            else:
                session.add(ConfigDB(name="role_id_assign", value=role_id))

            global g_DB_updated
            g_DB_updated = True

            self.role_id_assign = int(role_id)

            if role:
                await ctx.send(f"Inactivity role set to {role.name}", ephemeral=True)
            else:
                await ctx.send("Inactivity role disabled", ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to set inactivity role: {e}")
            try:
                session = await get_session()
                await session.rollback()
            except Exception as rollback_error:
                logger.error(f"Failed to rollback role setting: {rollback_error}")
            await ctx.send("Failed to set inactivity role", ephemeral=True)

    @module_group_setting.subcommand(
        "ignored_roles",
        sub_cmd_description="Set roles that will be ignored by the inactivity checker",
    )
    @interactions.slash_option(
        name="roles",
        description="The roles to ignore (comma-separated IDs, empty to clear)",
        required=False,
        opt_type=interactions.OptionType.STRING,
    )
    async def module_group_setting_ignored(
        self, ctx: interactions.SlashContext, roles: Optional[str] = None
    ) -> None:
        try:
            session = await get_session()
            role_ids = []
            if roles:
                try:
                    role_ids = [int(r.strip()) for r in roles.split(",") if r.strip()]
                except ValueError:
                    await ctx.send(
                        "Invalid role ID format. Please use comma-separated numbers.",
                        ephemeral=True,
                    )
                    return

            config_roles = ConfigRoles(roles=role_ids)
            roles_json = config_roles.model_dump_json()

            existing = await session.execute(
                sqlselect(ConfigDB).where(ConfigDB.name == "ignored_roles")
            )
            config = existing.scalar_one_or_none()

            if config:
                config.value = roles_json
            else:
                session.add(ConfigDB(name="ignored_roles", value=roles_json))

            global g_DB_updated
            g_DB_updated = True

            self.ignored_roles = role_ids

            if role_ids:
                await ctx.send(
                    f"Ignored roles set to: {', '.join(str(r) for r in role_ids)}",
                    ephemeral=True,
                )
            else:
                await ctx.send("Cleared ignored roles list", ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to set ignored roles: {e}")
            try:
                await session.rollback()
            except Exception as rollback_error:
                logger.error(
                    f"Failed to rollback ignored roles setting: {rollback_error}"
                )
            await ctx.send("Failed to set ignored roles", ephemeral=True)

    @module_group_setting.subcommand(
        "specific_roles",
        sub_cmd_description="Set specific roles to monitor for inactivity",
    )
    @interactions.slash_option(
        name="roles",
        description="The roles to monitor (comma-separated IDs, empty for all)",
        required=False,
        opt_type=interactions.OptionType.STRING,
    )
    async def module_group_setting_specific(
        self, ctx: interactions.SlashContext, roles: Optional[str] = None
    ) -> None:
        try:
            session = await get_session()
            role_ids = []
            if roles:
                try:
                    role_ids = [int(r.strip()) for r in roles.split(",") if r.strip()]
                except ValueError:
                    await ctx.send(
                        "Invalid role ID format. Please use comma-separated numbers.",
                        ephemeral=True,
                    )
                    return

            config_roles = ConfigRoles(roles=role_ids)
            roles_json = config_roles.model_dump_json()

            existing = await session.execute(
                sqlselect(ConfigDB).where(ConfigDB.name == "specific_roles")
            )
            config = existing.scalar_one_or_none()

            if config:
                config.value = roles_json
            else:
                session.add(ConfigDB(name="specific_roles", value=roles_json))

            global g_DB_updated
            g_DB_updated = True

            self.specific_roles = role_ids

            if role_ids:
                await ctx.send(
                    f"Specific roles set to: {', '.join(str(r) for r in role_ids)}",
                    ephemeral=True,
                )
            else:
                await ctx.send("Monitoring all roles (except ignored)", ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to set specific roles: {e}")
            try:
                await session.rollback()
            except Exception as rollback_error:
                logger.error(
                    f"Failed to rollback specific roles setting: {rollback_error}"
                )
            await ctx.send("Failed to set specific roles", ephemeral=True)

    @module_group_setting.subcommand(
        "execution_time",
        sub_cmd_description="Set the inactivity period before taking action",
    )
    @interactions.slash_option(
        name="hours",
        description="Number of hours of inactivity before taking action",
        required=True,
        opt_type=interactions.OptionType.INTEGER,
        min_value=1,
    )
    async def module_group_setting_time(
        self, ctx: interactions.SlashContext, hours: int
    ) -> None:
        try:
            session = await get_session()
            seconds = hours * 3600

            existing = await session.execute(
                sqlselect(ConfigDB).where(ConfigDB.name == "execution_time")
            )
            config = existing.scalar_one_or_none()

            if config:
                config.value = str(seconds)
            else:
                session.add(ConfigDB(name="execution_time", value=str(seconds)))

            global g_DB_updated
            g_DB_updated = True

            self.execution_time_second = seconds

            await ctx.send(f"Execution time set to {hours} hours", ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to set execution time: {e}")
            try:
                await session.rollback()
            except Exception as rollback_error:
                logger.error(
                    f"Failed to rollback execution time setting: {rollback_error}"
                )
            await ctx.send("Failed to set execution time", ephemeral=True)

    @module_group_setting.subcommand(
        "reset", sub_cmd_description="Reset all configuration to default values"
    )
    async def module_group_setting_reset(
        self,
        ctx: interactions.SlashContext,
    ) -> None:
        try:
            session = await get_session()
            await session.execute(sqldelete(ConfigDB))

            global g_DB_updated
            g_DB_updated = True

            self.role_id_assign = 0
            self.ignored_roles = []
            self.specific_roles = []
            self.execution_time_second = 86400

            await ctx.send(
                "All configuration settings have been reset to defaults",
                ephemeral=True,
            )

        except Exception as e:
            logger.error(f"Failed to reset configuration: {e}")
            try:
                await session.rollback()
            except Exception as rollback_error:
                logger.error(
                    f"Failed to rollback configuration reset: {rollback_error}"
                )
            await ctx.send("Failed to reset configuration settings", ephemeral=True)

    @module_base.subcommand(
        "pickup", sub_cmd_description="Check for and process inactive users immediately"
    )
    async def module_base_pickup(
        self,
        ctx: interactions.SlashContext,
    ) -> None:
        """Immediately check for and process inactive users"""
        if not h_data_initialised.is_set():
            await ctx.send(
                "Module is not initialized. Please initialize first.", ephemeral=True
            )
            return

        try:
            await ctx.defer(ephemeral=True)

            current_time = datetime.now().timestamp()
            inactive_users = []
            processed_count = 0

            for ut in Mem_UserTimes:
                time_since_last = current_time - ut.time
                if time_since_last >= self.execution_time_second:
                    inactive_users.append(ut.user)

            if not inactive_users:
                await ctx.send("No inactive users found.", ephemeral=True)
                return

            if len(self.bot.guilds) == 0:
                await ctx.send("Bot is not in any guilds.", ephemeral=True)
                return

            guild = self.bot.guilds[0]
            total = len(inactive_users)

            for user_id in inactive_users:
                try:
                    if member := await guild.fetch_member(user_id):
                        await execute_member(member)
                        processed_count += 1
                        logger.info(
                            f"Processed inactive member {member.display_name} ({member.id}) during pickup"
                        )
                except Exception as e:
                    logger.error(
                        f"Failed to process inactive member {user_id} during pickup: {e}"
                    )

            await ctx.send(
                f"Processed {processed_count} out of {total} inactive users.",
                ephemeral=True,
            )

        except Exception as e:
            logger.error(f"Failed to execute pickup command: {e}")
            await ctx.send(
                "An error occurred while processing inactive users.", ephemeral=True
            )

    @module_group_setting.subcommand(
        "show_config", sub_cmd_description="Show current configuration settings"
    )
    async def module_group_setting_show(
        self,
        ctx: interactions.SlashContext,
    ) -> None:
        """Display current configuration settings"""
        try:
            settings = [
                f"**Inactivity Role:** {self.role_id_assign if self.role_id_assign != 0 else 'Disabled'}",
                f"**Execution Time:** {self.execution_time_second // 3600} hours",
                f"**Ignored Roles:** {', '.join(str(r) for r in self.ignored_roles) if self.ignored_roles else 'None'}",
                f"**Monitored Roles:** {', '.join(str(r) for r in self.specific_roles) if self.specific_roles else 'All'}",
            ]

            await ctx.send(
                "Current Configuration:\n" + "\n".join(settings), ephemeral=True
            )

        except Exception as e:
            logger.error(f"Failed to show configuration: {e}")
            await ctx.send("Failed to retrieve configuration settings", ephemeral=True)
