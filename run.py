import asyncio
import os
import sys
import traceback
import uuid
from dataclasses import dataclass, field
from enum import Enum

import aiohttp
import discord
from discord.ext.commands import (
    BadArgument,
    NoPrivateMessage,
    PrivateMessageOnly,
    dm_only,
    guild_only,
)
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi_discord import DiscordOAuthClient
from hashids import Hashids

MAX_PLAYERS = 4

app = FastAPI()
bot = discord.Bot(intents=discord.Intents.all())
hashids = Hashids(salt=os.environ["HASHIDS_SALT"])

discord_oauth_client = DiscordOAuthClient(
    os.environ["DISCORD_CLIENT_ID"],
    os.environ["DISCORD_CLIENT_SECRET"],
    os.environ["DISCORD_REDIRECT_URI"],
    ("identify"),
)  # scopes


class KailleraError(Exception):
    def __init__(self, message: str):
        self.message = message


class AuthState(Enum):
    NOT_AUTH = 0
    AUTH_SUCCESS = 1


class GameStatus(Enum):
    IDLE = 0
    PLAYING = 1


@dataclass
class KailleraGame:
    players: list["KailleraUser"]
    id: int  # noqa: A003
    owner: "KailleraUser"
    rom_name: str
    thread: discord.Thread | None = None
    address: str | None = None
    status: GameStatus = GameStatus.IDLE
    created_game_thread_view: discord.ui.View | None = None
    create_game_interaction: discord.Interaction | None = None
    game_info_message: discord.Message | None = None


@dataclass
class KailleraUser:
    # Discord info
    id: int  # noqa: A003
    username: str
    display_name: str
    discriminator: str
    avatar: str
    mfa_enabled: bool
    locale: str
    flags: int
    public_flags: int
    avatar_decoration: str | None = None
    banner: str | None = None
    banner_color: str | None = None
    accent_color: str | None = None
    email: str | None = None
    premium_type: int | None = None

    # Kaillera info
    auth_state: AuthState = AuthState.NOT_AUTH
    game: KailleraGame | None = None
    game_list: list[str] = field(default_factory=list)
    # Set after creating a game
    ping: int | None = None
    # Set after starting a game
    player_number: int | None = None
    frame_delay: int | None = None


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, WebSocket] = {}

    async def connect(self, websocket: WebSocket, identifier: int):
        await websocket.accept()
        self.active_connections[identifier] = websocket

    def disconnect(self, websocket: WebSocket, identifier: int):
        if identifier in self.active_connections:
            self.active_connections.pop(identifier)

    async def send_personal_message(self, message: str, websocket: WebSocket):
        await websocket.send_text(message)

    async def broadcast(self, message: str):
        for connection in self.active_connections.values():
            await connection.send_text(message)


class BaseKailleraGameView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, error: Exception, item: discord.ui.Item, interaction: discord.Interaction):
        if isinstance(error, KailleraError):
            await interaction.response.send_message(content=error.message, ephemeral=True, delete_after=10.0)
        else:
            print(f"Ignoring exception in view {self} for item {item}:", file=sys.stderr)
            traceback.print_exception(error.__class__, error, error.__traceback__, file=sys.stderr)


# View passed when a user creates a new game
class CreatedGameThreadView(BaseKailleraGameView):
    def __init__(self, ctx):
        self.context = ctx
        super().__init__()

    @discord.ui.button(label="Join Game", style=discord.ButtonStyle.primary, custom_id="join_button")
    async def join_game_button_callback(self, button, interaction):
        global kaillera_users

        game_owner = kaillera_users.get(self.context.author.id)
        user = kaillera_users.get(interaction.user.id)
        if not user:
            raise KailleraError("You must be authenticated to use this command!")
        elif user.game:
            raise KailleraError("You are already in a game!")
        elif not game_owner or not game_owner.game:
            raise KailleraError("This game does not exist!")
        elif game_owner.game.rom_name not in user.game_list:
            raise KailleraError(f"You do not own this ROM {game_owner.game.rom_name}!")
        elif game_owner.game.status != GameStatus.IDLE:
            raise KailleraError("The game has already started!")
        elif len(game_owner.game.players) == MAX_PLAYERS:
            raise KailleraError("This game is full!")
        else:
            if user == game_owner or len(game_owner.game.players) == MAX_PLAYERS:
                button.disabled = True
            # on_thread_member_join hook will add the user to the game
            if not game_owner.game.thread:
                raise KailleraError("Error finding game!")

            await game_owner.game.thread.add_user(interaction.user)
            await interaction.response.edit_message(view=self)


class GameThreadView(BaseKailleraGameView):
    @discord.ui.button(label="Leave Game", style=discord.ButtonStyle.danger, custom_id="leave_button")
    async def leave_game_button_callback(self, button, interaction):
        global kaillera_users, authenticated_connection_manager

        user = kaillera_users.get(interaction.user.id)
        if not user:
            raise KailleraError("You must be authenticated to use this command!")
        elif not user.game:
            raise KailleraError("You don't have a game to leave!")
        else:
            websocket = authenticated_connection_manager.active_connections[interaction.user.id]
            await websocket.send_text("LEAVE GAME")
            if user == user.game.owner:
                if user.game.thread is not None:
                    await user.game.thread.delete()
                if user.game.create_game_interaction is not None:
                    await user.game.create_game_interaction.edit_original_response(view=None)
                for _user in user.game.players[:]:
                    if _user != user:
                        _user.game = None
            else:
                user.game.players.remove(user)
                if user.game.thread is not None:
                    await user.game.thread.remove_user(interaction.user)

            user.game = None
            await interaction.response.send_message(f"{interaction.user.mention} has left the game!")


class StartedGameThreadView(GameThreadView):
    @discord.ui.button(label="Drop Game", style=discord.ButtonStyle.danger, custom_id="drop_button")
    async def drop_game_button_callback(self, button, interaction):
        global kaillera_users, authenticated_connection_manager

        user = kaillera_users.get(interaction.user.id)
        if not user:
            raise KailleraError("You must be authenticated to use this command!")
        elif not user.game:
            raise KailleraError("You don't have a game to drop!")
        else:
            websocket = authenticated_connection_manager.active_connections[interaction.user.id]
            await websocket.send_text(f"DROP GAME{user.username}")
            user.game.status = GameStatus.IDLE

            joined_game_thread_view = JoinedGameThreadView()
            await interaction.response.edit_message(
                content=f"{interaction.user.mention} has dropped from the game!",
                view=joined_game_thread_view,
                embed=None,
            )


class JoinedGameThreadView(GameThreadView):
    @discord.ui.button(label="Start Game", style=discord.ButtonStyle.success, custom_id="start_button")
    async def start_game_button_callback(self, button, interaction):
        global kaillera_users, authenticated_connection_manager

        user = kaillera_users.get(interaction.user.id)
        if not user:
            raise KailleraError("You must be authenticated to use this command!")
        elif not user.game:
            raise KailleraError("You don't have a game to start!")
        elif user.game.owner != user:
            raise KailleraError("You must be the owner of this game to start it!")
        else:
            websocket = authenticated_connection_manager.active_connections[interaction.user.id]
            await websocket.send_text("START GAME")

            user.game.status = GameStatus.PLAYING

            embed = None
            if all(player.ping is not None and player.frame_delay is not None for player in user.game.players):
                embed = discord.Embed(title="Game Info", color=discord.Color.random())
                embed.add_field(
                    name="Ping",
                    value="\n".join(f"**{player.username}** {player.ping}ms" for player in user.game.players),
                )
                embed.add_field(
                    name="Frame Delay",
                    value="\n".join(f"**{player.username}** {player.frame_delay}" for player in user.game.players),
                )

            user.game.game_info_message = interaction.message
            start_game_thread_view = StartedGameThreadView()
            await interaction.response.edit_message(
                content=f"{interaction.user.mention} has started the game!",
                view=start_game_thread_view,
                embed=embed,
            )


authenticating_connection_manager: ConnectionManager = ConnectionManager()
authenticated_connection_manager: ConnectionManager = ConnectionManager()
kaillera_users: dict[int, KailleraUser] = {}


async def process_ws_data(websocket: WebSocket, data: str, user_id: int) -> None:
    global kaillera_users, authenticated_connection_manager

    user = kaillera_users[user_id]

    if data.startswith("LOGOUT"):
        authenticated_connection_manager.disconnect(websocket, user_id)
    elif data.startswith("GAME LIST"):
        user.game_list = data[9:].split(",")
    elif data.startswith("SERVER IP"):
        if user.game:
            user.game.address = data[9:]
        else:
            raise KailleraError("User not in a game!")
    elif data.startswith("PLAYER NUMBER"):
        user.player_number = int(data[13:])
        # print(f"{user.username} is player number {user.player_number}")
    elif data.startswith("FRAME DELAY"):
        user.frame_delay = int(data[11:])
        # print(f"{user.username} has frame delay {user.frame_delay}")
    elif data.startswith("USER PING"):
        user.ping = int(data[9:])
        # print(f"{user.username} has ping {user.ping}")

    if (
        user.game is not None
        and user.game.game_info_message is not None
        and all(player.ping is not None and player.frame_delay is not None for player in user.game.players)
        and not user.game.game_info_message.embeds
    ):
        embed = discord.Embed(title="Game Info", color=discord.Color.random())
        embed.add_field(
            name="Ping",
            value="\n".join(f"**{player.username}** {player.ping}ms" for player in user.game.players),
        )
        embed.add_field(
            name="Frame Delay",
            value="\n".join(f"**{player.username}** {player.frame_delay}" for player in user.game.players),
        )
        await user.game.game_info_message.edit(embed=embed)


async def remove_user_if_not_authenticated(user_id: int) -> None:
    global kaillera_users
    # User has 2 minutes to enter confirmation code
    await asyncio.sleep(120)
    user = kaillera_users.get(user_id)
    if user and user.auth_state != AuthState.AUTH_SUCCESS:
        del kaillera_users[user_id]


async def wait_on_user_authentication(user: KailleraUser, discord_user: discord.User) -> None:
    # User has 1 minutes to enter confirmation code
    async with asyncio.timeout(60):
        try:
            while user.auth_state != AuthState.AUTH_SUCCESS:
                await asyncio.sleep(1)
        except asyncio.TimeoutError:
            del kaillera_users[user.id]
            dm_channel = await bot.create_dm(discord_user)
            dm_msg = "Authentication timed out! You must restart the authentication process to continue"
            await dm_channel.send(dm_msg, delete_after=60.0)


@app.get("/callback")
async def discord_auth_callback(code: str) -> str:
    global kaillera_users

    dm_msg = "Use the /auth command to enter the authentication code from your kaillera client"
    token, _ = await discord_oauth_client.get_access_token(code)

    payload = {"Authorization": f"Bearer {token}"}

    try:
        async with aiohttp.ClientSession() as session:
            github_auth_url = "{url}/users/@me".format(url=os.environ["DISCORD_API_ENDPOINT"])
            async with session.get(github_auth_url, headers=payload) as response:
                user = await response.json()
                discord_user = bot.get_user(int(user["id"]))
                dm_channel = await bot.create_dm(discord_user)
                await dm_channel.send(dm_msg, delete_after=65.0)
                user["id"] = int(user["id"])

                try:
                    new_user = KailleraUser(**user)
                except TypeError:
                    print(f"Unable to create discord user {user}")
                    raise

                kaillera_users[user["id"]] = new_user

                bot.loop.create_task(wait_on_user_authentication(new_user, discord_user))
    except aiohttp.client_exceptions.ClientError as e:
        raise e
    return "Login Successful! You may now close this window."


@app.websocket("/ws/auth")
async def auth_websocket_endpoint(websocket: WebSocket) -> None:
    async with asyncio.timeout(120):
        global authenticating_connection_manager

        auth_id = uuid.uuid4().int
        await authenticating_connection_manager.connect(websocket, auth_id)
        try:
            while True:
                data = await websocket.receive_text()
                if data.startswith("START AUTH"):
                    oauth_login_url = (
                        "https://discord.com/oauth2/authorize?client_id={client_id}"
                        "&redirect_uri={redirect_uri}&scope=identify"
                        "&response_type=code".format(
                            client_id=os.environ["DISCORD_CLIENT_ID"],
                            redirect_uri=os.environ["DISCORD_REDIRECT_URI"],
                        )
                    )

                    await websocket.send_text(f"AUTH URL{oauth_login_url}")
                    await websocket.send_text(f"AUTH ID{hashids.encode(auth_id)}")
        except asyncio.TimeoutError:
            authenticating_connection_manager.disconnect(websocket, auth_id)
            await websocket.close()
        except WebSocketDisconnect:
            authenticating_connection_manager.disconnect(websocket, auth_id)


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int) -> None:
    global authenticated_connection_manager, kaillera_users

    user = kaillera_users.get(user_id)
    if not user:
        return
    await authenticated_connection_manager.connect(websocket, user_id)
    await websocket.send_text("GAME LIST")
    try:
        while True:
            data = await websocket.receive_text()
            await process_ws_data(websocket, data, user_id)
    except WebSocketDisconnect:
        disconnected_msg = (
            "Your emulator has disconnected from the server, you must reauthenticate to create and join games!"
        )
        discord_user = bot.get_user(user_id)
        dm_channel = await bot.create_dm(discord_user)
        await dm_channel.send(disconnected_msg, delete_after=15.0)
        authenticated_connection_manager.disconnect(websocket, user_id)
        if user:
            user = kaillera_users.pop(user_id)
            if user.game is not None and user.game.thread is not None and user == user.game.owner:
                try:
                    await user.game.thread.delete()
                except discord.NotFound:
                    pass
                if user.game.created_game_thread_view is None:
                    raise Exception("Unable to find created_game_thread_view for user game")
                user.game.created_game_thread_view.stop()
                user.game.created_game_thread_view.clear_items()
                if user.game.create_game_interaction is not None:
                    await user.game.create_game_interaction.edit_original_response(view=None)
            del user


async def get_user_game_list(ctx: discord.AutocompleteContext) -> list[str]:
    global kaillera_users
    user = kaillera_users.get(ctx.interaction.user.id)
    if not user:
        return []
    else:
        return user.game_list


async def get_game_owners(ctx: discord.AutocompleteContext) -> list[discord.Member]:
    global kaillera_users

    game_owners = []
    for member in ctx.interaction.guild.members:
        user = kaillera_users.get(member.id)
        if (
            user
            and user.id != ctx.interaction.user.id
            and user.game
            and user.game.status is GameStatus.IDLE
            and user.game.owner is user
        ):
            game_owners.append(member)

    return game_owners


# Confirmation code
@bot.slash_command(description="Enter the confirmation code from your kaillera client")
@dm_only()
async def auth(
    ctx: discord.ApplicationContext,
    auth_id: discord.Option(  # type: ignore
        str,
        description="The confirmation code from your kaillera client",  # noqa: F722
    ),
) -> None:
    global kaillera_users, authenticating_connection_manager

    try:
        decoded_auth_id = hashids.decode(auth_id)[0]
    except (IndexError, ValueError):
        raise BadArgument("Invalid authentication code")

    user = kaillera_users.get(ctx.author.id)
    if user and user.auth_state == AuthState.AUTH_SUCCESS:
        raise KailleraError("User has already been authenticated!")
    elif not auth_id:
        raise KailleraError("Please enter a valid auth id")
    elif decoded_auth_id not in authenticating_connection_manager.active_connections:
        raise KailleraError("User has not been authenticated yet or the time to authenticate has expired, try again")
    else:
        websocket = authenticating_connection_manager.active_connections.pop(decoded_auth_id)
        await websocket.send_text(f"USER ID{ctx.author.id}")
        await websocket.send_text("AUTH SUCCESS")
        user.auth_state = AuthState.AUTH_SUCCESS  # type: ignore
        await ctx.respond("Authentication successful!", delete_after=120.0)


# Create a game
@bot.slash_command(description="Create a game")
@guild_only()
async def creategame(
    ctx: discord.ApplicationContext,
    rom_name: discord.Option(  # type: ignore
        str,
        "Enter the name of the ROM you want to play",  # noqa: F722
        autocomplete=discord.utils.basic_autocomplete(get_user_game_list),
    ),
) -> None:
    global kaillera_users, authenticated_connection_manager

    user = kaillera_users.get(ctx.author.id)
    if not user:
        raise KailleraError("You must be authenticated to use this command!")
    elif user.game:
        raise KailleraError("You are already in a game!")
    elif not rom_name or rom_name not in user.game_list:
        raise KailleraError("Please enter a valid ROM name!")
    else:
        game_id = user.id
        user.game = KailleraGame(players=[user], id=game_id, owner=user, rom_name=rom_name)

        # If a user uses this command in a dm, we don't have a channel to create a thread in
        # so we skip creating a thread and just send a response to the user
        if not isinstance(ctx.channel, discord.PartialMessageable):
            thread = await ctx.channel.create_thread(
                name=f"{ctx.author.name} {rom_name}",
                type=discord.ChannelType.public_thread,
            )
            await thread.add_user(ctx.author)
            joined_game_thread_view = JoinedGameThreadView()
            await thread.send("Kaillera+ by: Agent 21", view=joined_game_thread_view)

            user.game.thread = thread
            created_game_thread_view = CreatedGameThreadView(ctx)

            user.game.created_game_thread_view = created_game_thread_view

            websocket = authenticated_connection_manager.active_connections[ctx.author.id]
            await websocket.send_text(f"CREATE GAME{rom_name}")

            user.game.create_game_interaction = await ctx.respond(
                f"{ctx.author.mention} has created a game! Rom name: {rom_name}",
                view=created_game_thread_view,
            )
        else:
            websocket = authenticated_connection_manager.active_connections[ctx.author.id]
            await websocket.send_text(f"CREATE GAME{rom_name}")
            await ctx.respond(f"{ctx.author.mention} has created a game! Rom name: {rom_name}")


# Leave a game
@bot.slash_command(description="Leave a game")
@guild_only()
async def leavegame(ctx: discord.ApplicationContext) -> None:
    global kaillera_users, authenticated_connection_manager

    user = kaillera_users.get(ctx.author.id)
    if not user:
        raise KailleraError("You must be authenticated to use this command!")
    elif not user.game:
        raise KailleraError("You don't have a game to leave!")
    else:
        websocket = authenticated_connection_manager.active_connections[ctx.author.id]
        await websocket.send_text("LEAVE GAME")

        if len(user.game.players) == MAX_PLAYERS:
            if not user.game.created_game_thread_view:
                raise Exception("Missing created_game_thread_view for user game")
            for child in user.game.created_game_thread_view.children:
                if child.custom_id == "join_button":
                    child.disabled = False

        if user == user.game.owner:
            if user.game.thread is not None:
                await user.game.thread.delete()

            for _user in user.game.players[:]:
                if _user != user:
                    _user.game = None

        else:
            user.game.players.remove(user)
            if user.game.thread is not None:
                await user.game.thread.remove_user(ctx.author)

        user.game = None


# Start game
@bot.slash_command(description="Start a game")
@guild_only()
async def startgame(ctx: discord.ApplicationContext) -> None:
    global kaillera_users, authenticated_connection_manager

    user = kaillera_users.get(ctx.author.id)
    if not user:
        raise KailleraError("You must be authenticated to use this command!")
    elif not user.game:
        raise KailleraError("You don't have a game to start!")
    elif user.game.owner != user:
        raise KailleraError("You are not the owner of this game!")
    elif user.game.status == GameStatus.PLAYING:
        raise KailleraError("Game has already started!")
    elif user.game.thread is None:
        raise KailleraError("Game thread has not been created!")
    elif ctx.channel != user.game.thread or ctx.channel.id != user.game.thread.id:
        raise KailleraError("You must be in the game thread to start the game!")
    else:
        websocket = authenticated_connection_manager.active_connections[ctx.author.id]
        await websocket.send_text("START GAME")
        user.game.status = GameStatus.PLAYING

        embed = None
        if all(player.ping is not None and player.frame_delay is not None for player in user.game.players):
            embed = discord.Embed(title="Game Info", color=discord.Color.random())
            embed.add_field(
                name="Ping",
                value="\n".join(f"**{player.username}** {player.ping}ms" for player in user.game.players),
            )
            embed.add_fieldplayers(
                name="Frame Delay",
                value="\n".join(f"**{player.username}** {player.frame_delay}" for player in user.game.players),
            )

        user.game.game_info_message = await user.game.thread.send(
            f"{ctx.author.mention} has started the game!", embed=embed
        )


# Join a game
@bot.slash_command(description="Join a game")
@guild_only()
async def joingame(
    ctx: discord.ApplicationContext,
    host: discord.Option(  # type: ignore
        discord.Member,
        "Select the user who's game you want to join",  # noqa: F722
        autocomplete=discord.utils.basic_autocomplete(get_game_owners),
    ),
) -> None:
    global kaillera_users, authenticated_connection_manager

    for user in kaillera_users.values():
        if host.id == user.id and user.game:
            game_id = user.game.id
            break
    else:
        raise KailleraError(
            "This game does not exist! Please make sure you entered the correct username and discriminator"
        )

    game_owner = kaillera_users.get(game_id)
    user = kaillera_users.get(ctx.author.id)  # type: ignore
    if not user:
        raise KailleraError("You must be authenticated to use this command!")
    elif user.game:
        raise KailleraError("You are already in a game!")
    elif not game_owner or not game_owner.game:
        raise KailleraError("This game does not exist!")
    elif game_owner.game.rom_name not in user.game_list:
        raise KailleraError(f"You do not own this ROM {game_owner.game.rom_name}!")
    else:
        if game_owner.game.thread is None:
            websocket = authenticated_connection_manager.active_connections[ctx.author.id]
            game_owner.game.players.append(user)
            user.game = game_owner.game

            await websocket.send_text(f"JOIN GAME{game_owner.game.address}")
            await websocket.send_text(f"ROM NAME{game_owner.game.rom_name}")

            await ctx.respond(
                f"{ctx.author.mention} has joined {host.mention}'s game!",
                delete_after=10.0,
            )
        else:
            await game_owner.game.thread.add_user(ctx.author)
            # on_thread_member_join hook will add the user to the game


@bot.event
async def on_application_command_error(
    ctx: discord.ApplicationContext,
    error: (Exception | discord.ApplicationCommandInvokeError | NoPrivateMessage | PrivateMessageOnly),
) -> None:
    if isinstance(error, discord.ApplicationCommandInvokeError):
        await ctx.interaction.response.send_message(
            content=str(error.original), delete_after=10.0, ephemeral=True  # type: ignore
        )
    elif isinstance(error, NoPrivateMessage):
        await ctx.interaction.response.send_message(
            content="This command cannot be used in private messages.",
            delete_after=10.0,
            ephemeral=True,
        )
    elif isinstance(error, PrivateMessageOnly):
        await ctx.interaction.response.send_message(
            content="This command can only be used in private messages.",
            delete_after=10.0,
            ephemeral=True,
        )
    else:
        raise error


@bot.event
async def on_thread_member_join(thread_member: discord.ThreadMember) -> None:
    global kaillera_users, authenticated_connection_manager

    thread_members = await thread_member.thread.fetch_members()
    # Unauthenticated users can join normal threads, but not kaillera game threads
    if thread_member.id not in kaillera_users and all(
        thread_member.id in kaillera_users for thread_member in thread_members
    ):
        await thread_member.thread.remove_user(thread_member)
        return
    for game_owner in kaillera_users.values():
        if (
            game_owner.game is not None
            and game_owner.game.thread is not None
            and thread_member.thread == game_owner.game.thread
            and kaillera_users[thread_member.id].game is None  # User cannot already be in a game
        ):
            user = kaillera_users.get(thread_member.id)
            if (
                not user
                or game_owner.game.status != GameStatus.IDLE
                or game_owner.game.rom_name not in user.game_list
                or len(game_owner.game.players) > MAX_PLAYERS
            ):
                # TODO: Send a message to the user with an error
                await thread_member.thread.remove_user(thread_member)
                return

            if thread_member.id not in (_user.id for _user in game_owner.game.players):
                websocket = authenticated_connection_manager.active_connections[thread_member.id]
                game_owner.game.players.append(user)
                user.game = game_owner.game

                await websocket.send_text(f"JOIN GAME{game_owner.game.address}")
                await websocket.send_text(f"ROM NAME{game_owner.game.rom_name}")
                print(f"{user.username} joined {game_owner.username}'s game. Server IP:{game_owner.game.address}")
                await thread_member.thread.send(f"{user.username} has joined the game!")


@bot.event
async def on_thread_member_remove(thread_member: discord.ThreadMember) -> None:
    global kaillera_users, authenticated_connection_manager

    if thread_member.id not in kaillera_users:
        return
    # Do nothing if the thread is not a kaillera game thread
    for game_owner in kaillera_users.values():
        if (
            game_owner.game is not None
            and game_owner.game.thread is not None
            and thread_member.thread == game_owner.game.thread
            and kaillera_users[thread_member.id].game is not None
        ):
            if thread_member.id in (user.id for user in game_owner.game.players):
                user = kaillera_users.get(thread_member.id)
                websocket = authenticated_connection_manager.active_connections[thread_member.id]
                await websocket.send_text("LEAVE GAME")
                if not user:
                    return
                if not user.game:
                    return
                if user == user.game.owner:
                    if user.game.thread is not None:
                        await user.game.thread.delete()
                    for _user in user.game.players[:]:
                        if _user != user:
                            _user.game = None
                else:
                    user.game.players.remove(user)
                    await thread_member.thread.send(f"{user.username} has left the game!")

                user.game = None
                return


async def run_bot() -> None:
    try:
        await bot.start(token=os.environ["DISCORD_TOKEN"])
    except KeyboardInterrupt:
        await bot.close()


@app.on_event("startup")
async def startup_event():
    loop = asyncio.get_event_loop()
    loop.create_task(run_bot())
