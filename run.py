import asyncio
import os
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Dict
from typing import List
from typing import Optional

import aiohttp
import discord
from fastapi import FastAPI
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi_discord import DiscordOAuthClient

MAX_PLAYERS = 4

app = FastAPI()
bot = discord.Bot(intents=discord.Intents.all())

discord_oauth_client = DiscordOAuthClient(
    os.environ["DISCORD_CLIENT_ID"],
    os.environ["DISCORD_CLIENT_SECRET"],
    os.environ["DISCORD_REDIRECT_URI"],
    ("identify"),
)  # scopes


class AuthState(Enum):
    NOT_AUTH = 0
    AUTH_SUCCESS = 1
    AUTH_FAILED = 2


class GameStatus(Enum):
    IDLE = 0
    STARTED = 1
    PLAYING = 2


@dataclass
class Game:
    players: List["DiscordUser"]
    id: int  # noqa: A003
    owner: "DiscordUser"
    rom_name: str
    thread: Optional[discord.Thread] = None
    address: Optional[str] = None
    status: GameStatus = GameStatus.IDLE


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
    elif data.startswith("DROP"):
        pass


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
                await dm_channel.send(dm_msg)
                user["id"] = int(user["id"])
                user_map.update({user["id"]: DiscordUser(**user)})
                asyncio.create_task(remove_user_if_not_authenticated(user["id"]))
    except aiohttp.client_exceptions.ClientError as e:
        raise e
    return "Login Successful! You may now close this window."


@app.websocket("/ws/auth")
async def auth_websocket_endpoint(websocket: WebSocket):
    global authenticating_connection_manager

    auth_id = uuid.uuid4().hex
    await authenticating_connection_manager.connect(websocket, int(auth_id, 16))
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
                await websocket.send_text(f"AUTH ID{auth_id}")
    except WebSocketDisconnect:
        authenticating_connection_manager.disconnect(websocket, int(auth_id, 16))


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    global authenticated_connection_manager

    await authenticated_connection_manager.connect(websocket, user_id)
    await websocket.send_text("GAME LIST")
    try:
        while True:
            data = await websocket.receive_text()
            await process_ws_data(websocket, data, user_id)
    except WebSocketDisconnect:
        authenticated_connection_manager.disconnect(websocket, user_id)
        if user_id in user_map:
            user = user_map.pop(user_id)
            if user.game.thread:
                await user.game.thread.delete()
            del user


# Confirmation code
@bot.slash_command()
async def cc(ctx: discord.ApplicationContext, auth_id: str):
    global user_map, authenticating_connection_manager

    if not auth_id:
        await ctx.respond("Please enter a valid auth id")
    elif int(auth_id, 16) not in authenticating_connection_manager.active_connections:
        await ctx.respond("User has not been authenticated yet or the time to authenticate has expired, try again")
    else:
        websocket = authenticating_connection_manager.active_connections.pop(int(auth_id, 16))
        await websocket.send_text(f"USER ID{ctx.author.id}")
        await websocket.send_text("AUTH SUCCESS")
        user_map[ctx.author.id].auth_state = AuthState.AUTH_SUCCESS
        await ctx.respond("Authentication successful!")


# Create a game
@bot.slash_command()
async def creategame(
    ctx: discord.ApplicationContext,
    rom_name: discord.Option(
        str,
        "Enter the name of the ROM you want to play",  # noqa: F722
        autocomplete=discord.utils.basic_autocomplete(lambda ctx: user_map[ctx.interaction.user.id].game_list),
    ),
):
    global user_map, authenticated_connection_manager

    user = user_map.get(ctx.author.id)
    if not user:
        await ctx.respond("You must be authenticated to use this command!")
    elif user.game:
        await ctx.respond("You are already in a game!")
    elif not rom_name or rom_name not in user.game_list:
        await ctx.respond("Please enter a valid ROM name!")
    else:
        # game_id = uuid.uuid4().hex
        game_id = user.id
        user.game = Game(players=[user], id=game_id, owner=user, rom_name=rom_name)

        # If a user uses this command in a dm, we don't have a channel to create a thread in
        # so we skip creating a thread and just send a response to the user
        if not isinstance(ctx.channel, discord.PartialMessageable):
            thread = await ctx.channel.create_thread(
                name=f"{ctx.author.name} {rom_name}", type=discord.ChannelType.public_thread
            )
            await thread.add_user(ctx.author)
            user.game.thread = thread

        websocket = authenticated_connection_manager.active_connections[ctx.author.id]
        await websocket.send_text(f"CREATE GAME{rom_name}")
        await ctx.respond(f"{ctx.author.mention} has created a game! Rom name: {rom_name} Game ID: {game_id}")


# Leave a game
@bot.slash_command()
async def leavegame(ctx: discord.ApplicationContext):
    global user_map, authenticated_connection_manager

    user = user_map.get(ctx.author.id)
    if not user:
        await ctx.respond("You must be authenticated to use this command!")
    elif not user.game:
        await ctx.respond("You don't have a game to leave!")
    else:
        websocket = authenticated_connection_manager.active_connections[ctx.author.id]
        await websocket.send_text("LEAVE GAME")
        if user == user.game.owner:
            if user.game.thread is not None:
                await user.game.thread.delete()
            for _user in user.game.players[:]:
                if _user != user:
                    _user.game = None
        else:
            user.game.players.remove(user)
            if user.game.thread is not None:
                discord_user = discord.Object(user.id)
                await user.game.thread.remove_user(discord_user)

        user.game = None
        await ctx.respond(f"{ctx.author.mention} has left their game!")


# Start game
@bot.slash_command()
async def startgame(ctx: discord.ApplicationContext):
    global user_map, authenticated_connection_manager

    user = user_map.get(ctx.author.id)
    if not user:
        await ctx.respond("You must be authenticated to use this command!")
    elif not user.game:
        await ctx.respond("You don't have a game to start!")
    elif user.game.owner != user:
        await ctx.respond("You are not the owner of this game!")
    else:
        websocket = authenticated_connection_manager.active_connections[ctx.author.id]
        await websocket.send_text("START GAME")
        user.game.status = GameStatus.STARTED
        await ctx.respond(f"{ctx.author.mention} has started the game!")


# Join a game
@bot.slash_command()
async def joingame(
    ctx: discord.ApplicationContext,
    game_id: discord.Option(
        str,
        "Enter the game id you want to join",  # noqa: F722
        autocomplete=discord.utils.basic_autocomplete(
            lambda ctx: [
                game.id for game in user_map[ctx.interaction.user.id].game_list if game.status != GameStatus.IDLE
            ]
        ),
    ),
):
    global user_map, authenticated_connection_manager
    game_id = int(game_id)

    game_owner = user_map.get(game_id)
    user = user_map.get(ctx.author.id)
    if not user:
        await ctx.respond("You must be authenticated to use this command!")
    elif user.game:
        await ctx.respond("You are already in a game!")
    elif not game_owner or not game_owner.game:
        await ctx.respond("This game does not exist!")
    elif game_owner.game.rom_name not in user.game_list:
        await ctx.respond(f"You do not have own this ROM {game_owner.game.rom_name}!")
    else:
        websocket = authenticated_connection_manager.active_connections[ctx.author.id]
        game_owner.game.players.append(user)
        user.game = game_owner.game

        await websocket.send_text(f"JOIN GAME{game_owner.game.address}")
        await websocket.send_text(f"ROM NAME{game_owner.game.rom_name}")

        if game_owner.game.thread is not None:
            discord_user = discord.Object(user.id)
            await game_owner.game.thread.add_user(discord_user)
        else:
            await ctx.respond(f"{ctx.author.mention} has joined game ID {game_id}")


@bot.event
async def on_thread_member_join(thread_member: discord.ThreadMember):
    thread_members = await thread_member.thread.fetch_members()
    # Users can join normal threads, but not kaillera game threads
    if thread_member.id not in user_map and all(
        thread_member.id for thread_member in thread_members if thread_member.id in user_map
    ):
        await thread_member.thread.remove_user(thread_member.id)
        return
    for game_owner in user_map.values():
        if (
            game_owner.game is not None
            and game_owner.game.thread is not None
            and thread_member.thread == game_owner.game.thread
            and user_map[thread_member.id].game is None  # This is a hack to make sure the user is not already in a game
        ):
            user = user_map.get(thread_member.id)
            if (
                not user
                or game_owner.game.status != GameStatus.IDLE
                or game_owner.game.rom_name not in user.game_list
                or len(game_owner.game.players) > MAX_PLAYERS
            ):
                # TODO: Send a message to the user with an error
                await thread_member.thread.remove_user(thread_member.id)
                return

            if thread_member.id not in [_user.id for _user in game_owner.game.players]:
                websocket = authenticated_connection_manager.active_connections[thread_member.id]
                game_owner.game.players.append(user)
                user.game = game_owner.game

                await websocket.send_text(f"JOIN GAME{game_owner.game.address}")
                await websocket.send_text(f"ROM NAME{game_owner.game.rom_name}")
                await thread_member.thread.send(f"{thread_member.name} has joined the game!")


@bot.event
async def on_thread_member_remove(thread_member: discord.ThreadMember):
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
                    if user.game.thread is not None:
                        discord_user = discord.Object(user.id)
                        await user.game.thread.remove_user(discord_user)
                    await thread_member.thread.send(f"{thread_member.name} has left the game!")

                user.game = None


@bot.user_command(name="Say Hello")
async def hi(ctx, user):
    await ctx.respond(f"{ctx.author.mention} says hello to {user.name}!")


async def run_bot():
    await bot.start(token=os.environ["DISCORD_TOKEN"])


asyncio.create_task(run_bot())
