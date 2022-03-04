import asyncio
import os
import sys
import traceback
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Dict
from typing import List
from typing import Optional

import aiohttp
import discord
from discord.ext.commands import BadArgument
from discord.ext.commands import dm_only
from discord.ext.commands import guild_only
from discord.ext.commands import NoPrivateMessage
from discord.ext.commands import PrivateMessageOnly
from fastapi import FastAPI
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
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
class Game:
    players: List["DiscordUser"]
    id: int  # noqa: A003
    owner: "DiscordUser"
    rom_name: str
    thread: Optional[discord.Thread] = None
    address: Optional[str] = None
    status: GameStatus = GameStatus.IDLE
    created_game_thread_view: Optional[discord.ui.View] = None
    create_game_interaction: Optional[discord.Interaction] = None
    game_info_message: Optional[discord.Message] = None


@dataclass
class DiscordUser:
    id: int  # noqa: A003
    username: str
    discriminator: str
    avatar: str
    mfa_enabled: bool
    locale: str
    flags: int
    public_flags: int
    banner: Optional[str] = None
    banner_color: Optional[str] = None
    accent_color: Optional[str] = None
    email: Optional[str] = None
    auth_state: Optional[AuthState] = AuthState.NOT_AUTH
    game: Optional[Game] = None
    game_list: List[str] = None
    premium_type: Optional[int] = None
    # Set after creating a game
    ping: Optional[int] = None
    # Set after starting a game
    player_number: Optional[int] = None
    frame_delay: Optional[int] = None


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, WebSocket] = {}

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
        global user_map

        game_owner = user_map.get(self.context.author.id)
        user = user_map.get(interaction.user.id)
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
            if len(game_owner.game.players) == MAX_PLAYERS - 1:
                button.disabled = True
            # on_thread_member_join hook will add the user to the game
            await game_owner.game.thread.add_user(interaction.user)
            await interaction.response.edit_message(view=self)


class GameThreadView(BaseKailleraGameView):
    @discord.ui.button(label="Leave Game", style=discord.ButtonStyle.danger, custom_id="leave_button")
    async def leave_game_button_callback(self, button, interaction):
        global user_map, authenticated_connection_manager

        user = user_map.get(interaction.user.id)
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
                    await user.game.create_game_interaction.edit_original_message(view=None)
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
        global user_map, authenticated_connection_manager

        user = user_map.get(interaction.user.id)
        if not user:
            raise KailleraError("You must be authenticated to use this command!")
        elif not user.game:
            raise KailleraError("You don't have a game to start!")
        elif user.game.owner != user:
            raise KailleraError("You are not the owner of this game!")
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
        global user_map, authenticated_connection_manager

        user = user_map.get(interaction.user.id)
        if not user:
            raise KailleraError("You must be authenticated to use this command!")
        elif not user.game:
            raise KailleraError("You don't have a game to start!")
        elif user.game.owner != user:
            raise KailleraError("You are not the owner of this game!")
        else:
            websocket = authenticated_connection_manager.active_connections[interaction.user.id]
            await websocket.send_text("START GAME")

            user.game.status = GameStatus.PLAYING
            embed = discord.Embed(title="Game Info", color=discord.Color.random())
            embed.add_field(
                name="Ping", value="\n".join(f"**{player.username}** {player.ping}ms" for player in user.game.players)
            )
            embed.add_field(
                name="Frame Delay",
                value="\n".join(f"**{player.username}** {player.frame_delay}" for player in user.game.players),
            )
            # start_game_thread_view = StartedGameThreadView()
            user.game.game_info_message = await user.game.thread.send(
                content=f"{interaction.user.mention} has started the game!", embed=embed
            )


authenticating_connection_manager = ConnectionManager()
authenticated_connection_manager = ConnectionManager()
user_map: Dict[int, DiscordUser] = {}


async def process_ws_data(websocket: WebSocket, data: str, user_id: int):
    global user_map, authenticated_connection_manager

    user = user_map[user_id]

    if data.startswith("LOGOUT"):
        authenticated_connection_manager.disconnect(websocket, user_id)
    elif data.startswith("GAME LIST"):
        user.game_list = data[9:].split(",")
    elif data.startswith("SERVER IP"):
        user.game.address = data[9:]
    elif data.startswith("PLAYER NUMBER"):
        user.player_number = int(data[13:])
        print(f"{user.username} is player number {user.player_number}")
    elif data.startswith("FRAME DELAY"):
        user.frame_delay = int(data[11:])
        print(f"{user.username} has frame delay {user.frame_delay}")
    elif data.startswith("USER PING"):
        user.ping = int(data[9:])
        print(f"{user.username} has ping {user.ping}")

    if user.game is not None and user.game.game_info_message is not None:
        embed = user.game.game_info_message.embeds[0]
        embed.clear_fields()
        embed.add_field(
            name="Ping", value="\n".join(f"**{player.username}** {player.ping}ms" for player in user.game.players)
        )
        embed.add_field(
            name="Frame Delay",
            value="\n".join(f"**{player.username}** {player.frame_delay}" for player in user.game.players),
        )
        await user.game.game_info_message.edit(embed=embed)


async def remove_user_if_not_authenticated(user_id: int):
    global user_map
    # User has 2 minutes to enter confirmation code
    await asyncio.sleep(120)
    user = user_map.get(user_id)
    if user and user.auth_state != AuthState.AUTH_SUCCESS:
        del user_map[user_id]


@app.get("/callback")
async def discord_auth_callback(code: str):
    global user_map

    dm_msg = "Use the /cc command to enter the authentication code from your kaillera client"
    token, _ = await discord_oauth_client.get_access_token(code)

    payload = {"Authorization": f"Bearer {token}"}

    try:
        async with aiohttp.ClientSession() as session:
            github_auth_url = "{url}/users/@me".format(url=os.environ["DISCORD_API_ENDPOINT"])
            async with session.get(github_auth_url, headers=payload) as response:
                user = await response.json()
                discord_user = discord.Object(user["id"])
                dm_channel = await bot.create_dm(discord_user)
                await dm_channel.send(dm_msg, delete_after=120.0)
                user["id"] = int(user["id"])
                user_map.update({user["id"]: DiscordUser(**user)})
                bot.loop.create_task(remove_user_if_not_authenticated(user["id"]))
    except aiohttp.client_exceptions.ClientError as e:
        raise e
    return "Login Successful! You may now close this window."


@app.websocket("/ws/auth")
async def auth_websocket_endpoint(websocket: WebSocket):
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
                        client_id=os.environ["DISCORD_CLIENT_ID"], redirect_uri=os.environ["DISCORD_REDIRECT_URI"]
                    )
                )

                await websocket.send_text(f"AUTH URL{oauth_login_url}")
                await websocket.send_text(f"AUTH ID{hashids.encode(auth_id)}")
    except WebSocketDisconnect:
        authenticating_connection_manager.disconnect(websocket, auth_id)


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    global authenticated_connection_manager, user_map

    user = user_map.get(user_id)
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
        discord_user = discord.Object(user.id)
        dm_channel = await bot.create_dm(discord_user)
        await dm_channel.send(disconnected_msg, delete_after=15.0)
        authenticated_connection_manager.disconnect(websocket, user_id)
        if user:
            user = user_map.pop(user_id)
            if user.game.thread:
                try:
                    await user.game.thread.delete()
                except discord.NotFound:
                    pass
                user.game.created_game_thread_view.stop()
                user.game.created_game_thread_view.clear_items()
            if user.game.create_game_interaction is not None:
                await user.game.create_game_interaction.edit_original_message(view=None)
            del user


async def get_user_game_list(ctx: discord.AutocompleteContext):
    global user_map
    user = user_map.get(ctx.interaction.user.id)
    if not user:
        return []
    else:
        return user.game_list


# Confirmation code
@bot.slash_command(description="Enter the confirmation code from your kaillera client")
@dm_only()
async def cc(ctx: discord.ApplicationContext, auth_id: str):
    global user_map, authenticating_connection_manager

    try:
        decoded_auth_id = hashids.decode(auth_id)[0]
    except (IndexError, ValueError):
        raise BadArgument("Invalid authentication code")

    user = user_map.get(ctx.author.id)
    if user.auth_state == AuthState.AUTH_SUCCESS:
        raise KailleraError("User has already been authenticated!")
    elif not auth_id:
        raise KailleraError("Please enter a valid auth id")
    elif decoded_auth_id not in authenticating_connection_manager.active_connections:
        raise KailleraError("User has not been authenticated yet or the time to authenticate has expired, try again")
    else:
        websocket = authenticating_connection_manager.active_connections.pop(decoded_auth_id)
        await websocket.send_text(f"USER ID{ctx.author.id}")
        await websocket.send_text("AUTH SUCCESS")
        user.auth_state = AuthState.AUTH_SUCCESS
        await ctx.respond("Authentication successful!", delete_after=120.0)


# Create a game
@bot.slash_command(description="Create a game")
@guild_only()
async def creategame(
    ctx: discord.ApplicationContext,
    rom_name: discord.Option(
        str,
        "Enter the name of the ROM you want to play",  # noqa: F722
        autocomplete=discord.utils.basic_autocomplete(get_user_game_list),
    ),
):
    global user_map, authenticated_connection_manager

    user = user_map.get(ctx.author.id)
    if not user:
        raise KailleraError("You must be authenticated to use this command!")
    elif user.game:
        raise KailleraError("You are already in a game!")
    elif not rom_name or rom_name not in user.game_list:
        raise KailleraError("Please enter a valid ROM name!")
    else:
        game_id = user.id
        user.game = Game(players=[user], id=game_id, owner=user, rom_name=rom_name)

        # If a user uses this command in a dm, we don't have a channel to create a thread in
        # so we skip creating a thread and just send a response to the user
        if not isinstance(ctx.channel, discord.PartialMessageable):
            thread = await ctx.channel.create_thread(
                name=f"{ctx.author.name} {rom_name}", type=discord.ChannelType.public_thread
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
                f"{ctx.author.mention} has created a game! Rom name: {rom_name}", view=created_game_thread_view
            )
        else:
            websocket = authenticated_connection_manager.active_connections[ctx.author.id]
            await websocket.send_text(f"CREATE GAME{rom_name}")
            await ctx.respond(f"{ctx.author.mention} has created a game! Rom name: {rom_name}")


# Leave a game
@bot.slash_command(description="Leave a game")
@guild_only()
async def leavegame(ctx: discord.ApplicationContext):
    global user_map, authenticated_connection_manager

    user = user_map.get(ctx.author.id)
    if not user:
        raise KailleraError("You must be authenticated to use this command!")
    elif not user.game:
        raise KailleraError("You don't have a game to leave!")
    else:
        websocket = authenticated_connection_manager.active_connections[ctx.author.id]
        await websocket.send_text("LEAVE GAME")

        if len(user.game.players) == MAX_PLAYERS:
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
async def startgame(ctx: discord.ApplicationContext):
    global user_map, authenticated_connection_manager

    user = user_map.get(ctx.author.id)
    if not user:
        raise KailleraError("You must be authenticated to use this command!")
    elif not user.game:
        raise KailleraError("You don't have a game to start!")
    elif user.game.owner != user:
        raise KailleraError("You are not the owner of this game!")
    elif user.game.status is GameStatus.PLAYING:
        raise KailleraError("Game has already started!")
    elif user.game.thread is None:
        raise KailleraError("Game thread has not been created!")
    elif ctx.channel != user.game.thread or ctx.channel.id != user.game.thread.id:
        raise KailleraError("You must be in the game thread to start the game!")
    else:
        websocket = authenticated_connection_manager.active_connections[ctx.author.id]
        await websocket.send_text("START GAME")
        user.game.status = GameStatus.PLAYING
        embed = discord.Embed(title="Game Info", color=discord.Color.green())
        embed.add_field(
            name="Ping", value="\n".join(f"**{player.username}** {player.ping}ms" for player in user.game.players)
        )
        embed.add_field(
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
    username_and_discriminator: discord.Option(
        str,
        "Enter the username (including the # discriminator) you want to join",  # noqa: F722
        autocomplete=discord.utils.basic_autocomplete(
            lambda ctx: [
                f"{user.username}#{user.discriminator}"
                for user in user_map.values()
                if user.game and user.game.status == GameStatus.IDLE and user.id != ctx.interaction.user.id
            ]
        ),
    ),
):
    global user_map, authenticated_connection_manager

    for user in user_map.values():
        if f"{user.username}#{user.discriminator}" == username_and_discriminator:
            game_id = user.game.id
            break
    else:
        raise KailleraError(
            "This game does not exist! Please make sure you entered the correct username and discriminator"
        )
        return

    game_owner = user_map.get(game_id)
    user = user_map.get(ctx.author.id)
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

            await ctx.respond(f"{ctx.author.mention} has joined {game_owner.username}'s game!")
        else:
            await game_owner.game.thread.add_user(ctx.author)
            # on_thread_member_join hook will add the user to the game


@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: Exception):
    if isinstance(error, discord.ApplicationCommandInvokeError):
        await ctx.interaction.response.send_message(content=str(error.original), delete_after=10.0, ephemeral=True)
    elif isinstance(error, NoPrivateMessage):
        await ctx.interaction.response.send_message(
            content="This command cannot be used in private messages.", delete_after=10.0, ephemeral=True
        )
    elif isinstance(error, PrivateMessageOnly):
        await ctx.interaction.response.send_message(
            content="This command can only be used in private messages.", delete_after=10.0, ephemeral=True
        )
    else:
        raise error


@bot.event
async def on_thread_member_join(thread_member: discord.ThreadMember):
    global user_map, authenticated_connection_manager

    thread_members = await thread_member.thread.fetch_members()
    # Unauthenticated users can join normal threads, but not kaillera game threads
    if thread_member.id not in user_map and all(
        thread_member.id for thread_member in thread_members if thread_member.id in user_map
    ):
        await thread_member.thread.remove_user(thread_member)
        return
    for game_owner in user_map.values():
        if (
            game_owner.game is not None
            and game_owner.game.thread is not None
            and thread_member.thread == game_owner.game.thread
            and user_map[thread_member.id].game is None  # User cannot already be in a game
        ):
            user = user_map.get(thread_member.id)
            if (
                not user
                or game_owner.game.status != GameStatus.IDLE
                or game_owner.game.rom_name not in user.game_list
                or len(game_owner.game.players) > MAX_PLAYERS
            ):
                # TODO: Send a message to the user with an error
                await thread_member.thread.remove_user(thread_member)
                return

            if thread_member.id not in [_user.id for _user in game_owner.game.players]:
                websocket = authenticated_connection_manager.active_connections[thread_member.id]
                game_owner.game.players.append(user)
                user.game = game_owner.game

                await websocket.send_text(f"JOIN GAME{game_owner.game.address}")
                await websocket.send_text(f"ROM NAME{game_owner.game.rom_name}")
                await thread_member.thread.send(f"{user.username} has joined the game!")


@bot.event
async def on_thread_member_remove(thread_member: discord.ThreadMember):
    global user_map, authenticated_connection_manager

    if thread_member.id not in user_map:
        return
    # Do nothing if the thread is not a kaillera game thread
    for game_owner in user_map.values():
        if (
            game_owner.game is not None
            and game_owner.game.thread is not None
            and thread_member.thread == game_owner.game.thread
            and user_map[thread_member.id].game is not None
        ):
            if thread_member.id in [user.id for user in game_owner.game.players]:
                user = user_map.get(thread_member.id)
                websocket = authenticated_connection_manager.active_connections[thread_member.id]
                await websocket.send_text("LEAVE GAME")
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


# @bot.user_command(name="Say Hello")
# async def hi(ctx, user):
#     await ctx.respond(f"{ctx.author.mention} says hello to {user.name}!")


async def run_bot():
    await bot.start(token=os.environ["DISCORD_TOKEN"])


asyncio.create_task(run_bot())
