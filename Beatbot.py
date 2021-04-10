#Beatbot.py
import os
import math
import itertools
import functools
import random
import discord
import youtube_dl
import asyncio
import datetime
import time
from dotenv import load_dotenv
from async_timeout import timeout
from youtube_dl import YoutubeDL
from discord.ext import commands
from discord.utils import get
from asyncio import run_coroutine_threadsafe

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD = os.getenv("DISCORD_GUILD")

bot = discord.Client()
bot = commands.Bot(command_prefix="!", case_insensitive=True, owner_id = 265781552926031872)
bot.remove_command('help')
bot._skip_check = lambda x, y: False

youtube_dl.utils.bug_reports_message = lambda: ''

class VoiceError(Exception):
    pass

class YTDLError(Exception):
    pass

class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': False,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError('Impossible de trouver quoi que ce soit qui corresponde à `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries'][0]:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError('Impossible de trouver quoi que ce soit qui corresponde à `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Impossible de récupérer `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError('Impossible de récupérer les correspondances pour `{}`'.format(webpage_url))

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)

class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(colour=discord.Colour.green(),
                               title='En cours de lecture',
                               description='```css\n{0.source.title}\n```'.format(self),
                               timestamp= datetime.datetime.now(),
                               color=discord.Color.blurple())
                 .add_field(name='Durée', value=self.source.duration, inline=False)
                 .add_field(name='Demandé par', value=self.requester.mention)
                 .add_field(name='Lien direct', value='[Ici]({0.source.url})'.format(self))
                 .set_thumbnail(url=self.source.thumbnail)
                 .set_footer(text='BeatBot Beta 1.0.7'))

        return embed

class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)
    
    def remove(self, index: int):
        del self._queue[index]

    def removelast(self):
        del self._queue[-1]

class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()
        self.exists = True

        self._loop = False
        self._volume = 0.15
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                try:
                    async with timeout(120):  # 2 minutes
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    ciao = [
        'Ciao bande de nazes! <:s0cul:730551865334562834>\n(2 minutes d\'inactivité)',
        'Vers l\'infini et au delà! <:butplug:646778028071845949>\n(2 minutes d\'inactivité)',
        'Bon, bah, je me tire 🚽\n(2 minutes d\'inactivité)',
        'A plus dans le bus! 🖕\n(2 minutes d\'inactivité)',
        'Hasta la (windows) vista, Baby! 🏍️\n(2 minutes d\'inactivité)', 
        'I\'ll be back! 🔫\n(2 minutes d\'inactivité)', 
        'J\'ai autre chose à foutre que de rester planter là à vous mater vous toucher la bite... 🧻\n(2 minutes d\'inactivité)',
        'Bon je me fais un bréxit, si vous me cherchez je suis pas loin! <:ah:647451727867674628>\n(2 minutes d\'inactivité)'
    ]
                    reponse_ciao = random.choice(ciao)
                    await self._ctx.send(reponse_ciao)
                    print('Déconnection pour inactivité')
                    self.bot.loop.create_task(self.stop())
                    self.exists = False
                    return

                self.current.source.volume = self._volume
                self.voice.play(self.current.source, after=self.play_next_song)
                await self.current.source.channel.send(embed=self.current.create_embed())
            
            #Si c'est loopé
            elif self.loop:
                self.now = discord.FFmpegPCMAudio(self.current.source.stream_url, **YTDLSource.FFMPEG_OPTIONS)
                self.voice.play(self.now, after=self.play_next_song)
            
            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            await bot.change_presence(status=discord.Status.idle) #Change le status du bot lors de la déco
            self.voice = None

class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state or not state.exists:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage("Tu m'as pris pour ton DJ perso ? Pas de ça en DM !.")

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send('Une erreur est survenue: {}'.format(str(error)))

    #COMMANDE JOIN
    @commands.command(name='join', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()
        print(f'Connecté au chan {destination}\n')

    #COMMANDE D'INVOCATION
    @commands.command(name='summon')
    async def _summon(self, ctx: commands.Context, *, channel: discord.VoiceChannel = None):

        if not channel and not ctx.author.voice:
            raise VoiceError("Tu n'es dans aucun canal et n'en à pas spécifié")

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    #COMMANDE STOP (vide la liste et quitte le chan)
    @commands.command(name='leave', aliases=['stop','stp','tg'])
    async def _leave(self, ctx: commands.Context):

        if not ctx.voice_state.voice:
            return await ctx.send('Non connecté à un canal vocal.')

        await ctx.voice_state.stop()
        await bot.change_presence(status=discord.Status.idle)
        del self.voice_states[ctx.guild.id]
        print(f"Déconnection à la demande de l'utilisateur")

    #COMMANDE VOLUME
    @commands.command(aliases=['vol','v'])
    async def volume(self, ctx, volume: int):
        if ctx.voice_client is None:
            return await ctx.send("Non connecté à un canal vocal.")

        ctx.voice_client.source.volume = volume / 100
        await ctx.send("Volume réglé à {}%".format(volume))
        print("Volume réglé à {}%".format(volume))

    #COMMANDE NOW
    @commands.command(name='now', aliases=['nw', 'playing'])
    async def _now(self, ctx: commands.Context):

        await ctx.send(embed=ctx.voice_state.current.create_embed())
    
    #COMMANDE PAUSE
    @commands.command(name='pause', aliases=['pa','pose','pase'])
    async def _pause(self, ctx: commands.Context):

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')

    #COMMANDE RESUME
    @commands.command(name='resume', aliases=['r', 'res', 'reprise','rep'])
    async def _resume(self, ctx: commands.Context):

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')

    #COMMANDE NEXT
    @commands.command(name='skip', aliases=['n','nxt','next'])
    async def _skip(self, ctx: commands.Context):

        if not ctx.voice_state.is_playing:
            return await ctx.send('Aucune lecture en cours...')

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >=1:
                await ctx.message.add_reaction('⏭')
                ctx.voice_state.skip()
            else:
                await ctx.send('Vote ajouté, actuellement **{}/3**'.format(total_votes))

        else:
            await ctx.send('Tu as déjà voté')

    #COMMANDE LIST
    @commands.command(name='queue', aliases=['list','ll','ls'])
    async def _queue(self, ctx: commands.Context, *, page: int = 1):

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("File d'attente vide.")

        items_per_page = 20
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(description='**{} tracks:**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Page {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    #COMMANDE SHUFFLE
    @commands.command(name='shuffle', aliases=['shfl'])
    async def _shuffle(self, ctx: commands.Context):

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("File d'attente vide.")

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction('✅')

    #COMMANDE REMOVE
    @commands.command(name='remove', aliases=['rm'])
    async def _remove(self, ctx: commands.Context, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("File d'attente vide.")

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('✅')
        
    #COMMANDE REMOVE LAST
    @commands.command(name='removelast', aliases=['rml'])
    async def _removelast(self, ctx: commands.Context):
    
        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("File d'attente vide.")

        ctx.voice_state.songs.removelast()
        await ctx.message.add_reaction('✅')
        await ctx.send("Suppression du dernier morceau ajouté")

    #COMMANDE LOOP (Bug volume)
    @commands.command(name='loop')
    async def _loop(self, ctx: commands.Context):
        """Loops the currently playing song.
        Invoke this command again to unloop the song.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send("Aucune lecture en cours.")

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.message.add_reaction('✅')

    #COMMANDE PLAY
    @commands.command(name='play',aliases=["p","pl"])
    async def _play(self, ctx: commands.Context, *, search: str):

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except YTDLError as e:
                await ctx.send('Une erreur s\'est produite lors du traitement de cette demande: {}'.format(str(e)))
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                await ctx.send('{} ajouté'.format(str(source)))
                await ctx.message.add_reaction('💾')
                await bot.change_presence(status=discord.Status.online, activity=discord.Game("🎶!help🎹!now🎶")) #Change le status du bot

    #COMMANDE PLAYLIST
    #Lecture d'un fichier et envoi d'un message par ligne
    @commands.command(name='pl1')
    async def pl1(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/01_[Fanatic12000]_abduction01") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)
    
    @commands.command(name='pl2')
    async def pl2(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/02_[dgoHn]_Monega_EP") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)
  
    @commands.command(name='pl3')
    async def pl3(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/03_[Synth_Alien]_Das_Augas_ó_Alén") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl4')
    async def pl4(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/04_[Shinra]_Reverie_EP") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)
    
    @commands.command(name='pl5')
    async def pl5(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/05_[Shinra]_Supernova_EP") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl6')
    async def pl6(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/06_[VA]_Wave_Function_Collapse") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)  

    @commands.command(name='pl7')
    async def pl7(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/07_[Magic_Sword]_Endless") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)   
    
    @commands.command(name='pl8')
    async def pl8(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/08_[Magic_Sword]_Volume_1") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)
    
    @commands.command(name='pl9')
    async def pl9(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/09_[D_Arcangelo]_Tweaking_Paper_EP") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl10')
    async def pl10(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/10_[Voiron]_Drill_n_Voiron") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl11')
    async def pl11(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/11_[Kavinsky]_1986_EP") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl12')
    async def pl12(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/12_[Kavinsky]_Nightcall_EP") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl13')
    async def pl13(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/13_[RTR]_Symmetriades") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl14')
    async def pl14(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/14_[Kiemsa]_Eaux_Troubles") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl15')
    async def pl15(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/15_[Carpenter_Brut]_EP_III") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl16')
    async def pl16(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/16_[MasterBootRecord]_Virtuaverse_OST") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl17')
    async def pl17(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/17_[ZC-ELEC001]_Escape_From_Andromeda") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl18')
    async def pl18(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/18_[ZC-ELEC002]_The_Space_Gate") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl19')
    async def pl19(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/19_[ZC-303001]_303_First_Pattern") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl20')
    async def pl20(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/20_[MasterBootRecord]_INTERRUPT_REQUEST") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl21')
    async def pl21(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/21_[MasterBootRecord]_INTERNET_PROTOCOL") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)

    @commands.command(name='pl22')
    async def pl22(self, ctx: commands.Context):
        
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        with open("./playlist/22_[Velum Break]_Cloaca_EP") as f:
            data = f.readlines()
        for i in data:
                await ctx.send(f"!p {i}")
                time.sleep(1)
                    
    #Liste les playlist dispo 
    @commands.command(name='pll')
    async def pll(self, ctx: commands.Context, *, page: int = 1):

        playlist = []
        for x in (os.listdir('./playlist')):
            playlist.append(x)
            playlist.sort()
        
        """items_per_page = 5
        pages = math.ceil(len(playlist) / items_per_page)
        
        start = (page - 1) * items_per_page
        end = start + items_per_page
        
        for x in (os.listdir('./playlist')):
            await ctx.send(f"```css\n{x}```")"""
        embed = (discord.Embed(title="Playlist Dispos", description='\n'.join(playlist), color=0xff8040)
             .set_footer(text=f"Quémandé par: {ctx.author}"))
             #.set_footer(text='Page {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)


    #Check si l'utilisateur qui demande la lecture est dans un canal vocal
    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError("Vous n'êtes connecté à aucun canal vocal.")

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError("BeatBot est déjà dans un canal vocal.")

#EVENTS ###############################################################

# M'affiche dans le terminal les serveurs connectés, list les users et passe le bot en Idle lorsque le bot est chargé/prêt
@bot.event
async def on_ready():
    for guild in bot.guilds:
        if guild.name == GUILD:
            break

    print(
        f"{bot.user} BeatBot s'est connecté à:\n"
        f"{guild.name}(id: {guild.id})"
    )

    members = "\n - ".join([member.name for member in guild.members])
    print(f"List des membres de {guild.name}:\n===============================\n- {members}\n===============================\n")
    print(f"\nBot prêt!")
    await bot.change_presence(status=discord.Status.idle)


#Réponse en cas de mauvais commande
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        nop_e = discord.Embed(
            colour=discord.Colour.red(),
            title=f"RTFM!")
        nop_e.add_field(name="Apprendre à taper...", value=f"Si on m'avait filé une papoule à\nchaque fois que tu te plantes,\nje serais riche.", inline=True)
        nop_e.add_field(name="Un petit coup de pouce ?", value=f"```css\n!help```")
        await ctx.send(embed=nop_e)


@bot.event
async def on_message(message):
    ctx = await bot.get_context(message)
    await bot.invoke(ctx)


#LISTENERS ###############################################################

@bot.listen()
async def on_message(message):
    if "half life" in message.content.lower():
        hl_quote =[
            'Where are we taking this Freeman guy?','We remember the Freeman. We are coterminous.','Time, Dr. Freeman? Is it really that … time again? It seems as if you only just arrived.', 
            'Gordon? Alyx? I don\'t believe it! How the hell\'d you get out of the Citadel?', 'prepare for unforeseen consequences', 
            'The cake is a lie, heu désolé je me suis planté de jeu', 'Bienvenue à Black Mesa', 'Réveillez vous Docteur Freeman ! Sentez vous l\'odeur des cendres?!']
        reponse = random.choice(hl_quote)
        await message.channel.send(reponse)

@bot.listen()
async def on_message(message):  
    if "connard" in message.content:
        connard_answ =[
            'Oui?','C\'est mon deuxieme prénom','On m\'appelle?', 'Tu te trompes de personne, je ne suis pas <@265781552926031872>', 'C\'est toi le connard non mais!', 
            'Qui m\'invoque?', 'Pourquoi tu t\'appelles toi même ?', 'Tu m\'as pris pour <@268109733666226176> ou quoi ?']
        reponse = random.choice(connard_answ)
        await message.channel.send(reponse)

# COMMANDES DIVERSES ###########################################################

# Custom Help
@bot.command(name='help', aliases=['halp','hemp','hrlp','jelp','hellp','h'])
async def help(ctx):
    author = ctx.message.author

    help_e = discord.Embed(colour=discord.Colour.green(), timestamp=ctx.message.created_at,
                            title=f"Me voici pour t'aider {author} !")
    help_e.add_field(name='!help', value=f"Affiche cette aide\n```css\n!help```", inline=False)
    help_e.add_field(name='!whois', value=f"Affiche les infos utilisateur\nPrend le pseudo en parametre\npour afficher les infos d'un autre membre ```css\n!whois @pseudo```", inline=False)
    help_e.add_field(name='COMMANDES MULTIMEDIA', value=f"-Play ```css\n!play YT/BC/SC/'strings'```\n-Pause ```css\n!pause```\n-Reprendre ```css\n!resume```\n-Suivant ```css\n!next```\n-Stop ```css\n!stop```\n-Monter/Baisser le volume ```css\n!vol int [0-100]```\n-Looper la lecture ```css\n!loop```\n-Demander la lecture en cours ```css\n!now```\n-Montrer le contenu de la playlist ```css\n!list```\n-Supprimer une track ```css\n!rm int [track index]```\n-Supprimer le dernier morceau ajouté ```css\n!rml```\n-Mélanger la liste de lecture```css\n !shuffle```\n-Lancer les custom playlist```css\n !pl1 / !pl2 / !pl3```",inline=True)
    help_e.set_footer(text='BeatBot Beta 1.0.6')
    await ctx.send(embed=help_e)

# Infos Utilisateur
@bot.command(name='whois',aliases=['wois', 'whoi', 'hois','wohis'])
async def userinfo(ctx, member: discord.Member = None):
    if not member:
        member = ctx.message.author
    roles = [role for role in member.roles[1:]]
    whois_e = discord.Embed(
        colour=discord.Colour.orange(), 
        timestamp=ctx.message.created_at,
        title=f"Mais qui est {member} ?")
    whois_e.set_thumbnail(url=member.avatar_url)
    whois_e.set_footer(text=f"Quémandé par: {ctx.author}")

    whois_e.add_field(name="ID:", value=member.id, inline=True)
    whois_e.add_field(name="Pseudo:", value=member.display_name)

    whois_e.add_field(name="Est sur Discord depuis:", value=member.created_at.strftime("%d %b %Y à %H:%M")) 
    whois_e.add_field(name="A rejoint le serveur:", value=member.joined_at.strftime("%d %b %Y à %H:%M"))

    whois_e.add_field(name="Rôles:", value="\n".join([role.mention for role in roles]))
    whois_e.add_field(name="Rôle le plus puissant:", value=member.top_role.mention)
    await ctx.send(embed=whois_e)


# Nettoyage du chan txt
@bot.command(name='purge')
@commands.has_role(707258582017638481) #Limite au Role s0carole
async def purge(ctx, messages: int = 10):
    
        if messages > 1000:
            return ctx.send("Stop c'est un peu trop ! Pas au dela de 1000!")
        await ctx.channel.purge(limit=messages)
        removed = messages
        await ctx.send(f"Suppression de {removed} messages")
        print(f"Suppression de {removed} messages")


bot.add_cog(Music(bot))
bot.run(TOKEN)
