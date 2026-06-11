import audioop_shim  # Import shim FIRST to fix Python 3.14 compatibility
import discord
from discord.ext import commands
import random
import json
import os
import asyncio
import hashlib
import hmac
import secrets
import re
import math
from datetime import datetime, timezone, timedelta
from images import (
    balance_card, coinflip_card, dice_card, slots_card,
    roulette_card, blackjack_card, addbal_card
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.invites = True

bot = commands.Bot(command_prefix='.', intents=intents, help_command=None)

DB_FILE      = 'bot/user_data.json'
active_mines = {}
active_bj    = {}
invite_cache = {}   # guild_id -> {code: uses}

POINTS_TO_USD = 0.0037

RANKS = [
    (0,         "🥉 Bronze",   0xCD7F32),
    (5_000,     "🥈 Silver",   0xC0C0C0),
    (25_000,    "🥇 Gold",     0xFFD700),
    (100_000,   "💎 Platinum", 0x64C8FF),
    (500_000,   "👑 Diamond",  0xB464FF),
    (2_000_000, "⚡ VIP",      0xFF5000),
]
RANK_KEYS = ["bronze", "silver", "gold", "platinum", "diamond", "vip"]

def get_rank_info(total_wagered):
    rank = RANKS[0]; rank_idx = 0
    for i, entry in enumerate(RANKS):
        if total_wagered >= entry[0]:
            rank = entry; rank_idx = i
    next_rank = RANKS[rank_idx + 1] if rank_idx + 1 < len(RANKS) else None
    return rank, next_rank

def rank_key(rank_name):
    return rank_name.split()[-1].lower()

def fmt(points):
    usd = points * POINTS_TO_USD
    return f"R${points:,} (≈ ${usd:.2f})"

# ── Data ────────────────────────────────────────────────────────────────

def load_data():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DB_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def get_user(user_id):
    data = load_data(); uid = str(user_id)
    if uid not in data:
        data[uid] = {
            'balance': 0,
            'stats': {'wins': 0, 'losses': 0, 'total_wagered': 0, 'total_lost': 0},
            'last_daily': None, 'last_monthly': None,
            'wager_at_last_monthly': 0, 'rakeback_available': 0.0, 'clan': None,
            'bonus_received': 0, 'tips_sent': 0, 'tips_received': 0, 'total_withdrawn': 0,
        }
        save_data(data)
    u = data[uid]; changed = False
    for key, default in [
        ('last_daily', None), ('last_monthly', None), ('wager_at_last_monthly', 0),
        ('rakeback_available', 0.0), ('clan', None), ('bonus_received', 0),
        ('tips_sent', 0), ('tips_received', 0), ('total_withdrawn', 0),
        ('daily_invites', 0), ('daily_invites_date', None), ('total_invites', 0),
    ]:
        if key not in u: u[key] = default; changed = True
    if 'total_lost' not in u.get('stats', {}):
        u.setdefault('stats', {})['total_lost'] = 0; changed = True
    if changed: save_data(data)
    return data, uid

def get_user_balance(user_id):
    data, uid = get_user(user_id); return data[uid]['balance']

def resolve_bet(amount_str, balance):
    """Convert 'all', 'half', or a number string to an integer bet amount."""
    s = str(amount_str).lower().strip()
    if s == 'all':
        return balance
    if s == 'half':
        return max(1, balance // 2)
    try:
        return int(s)
    except ValueError:
        return None

def set_user_balance(user_id, amount):
    data, uid = get_user(user_id)
    data[uid]['balance'] = max(0, amount); save_data(data)

def add_to_stats(user_id, result, wager):
    data, uid = get_user(user_id); s = data[uid]['stats']
    s['total_wagered'] += wager
    if result:
        s['wins'] += 1
    else:
        s['losses'] += 1
        s['total_lost'] = s.get('total_lost', 0) + wager
        data[uid]['rakeback_available'] = data[uid].get('rakeback_available', 0.0) + wager * 0.002
    save_data(data)

def get_config():
    return load_data().get('__config__', {})

def save_config(cfg):
    data = load_data(); data['__config__'] = cfg; save_data(data)

def get_codes():
    return load_data().get('__codes__', {})

def save_codes(codes):
    data = load_data(); data['__codes__'] = codes; save_data(data)

def get_clans():
    return load_data().get('__clans__', {})

def save_clans(clans):
    data = load_data(); data['__clans__'] = clans; save_data(data)

def send_image(buf, filename='result.png'):
    buf.seek(0); return discord.File(buf, filename=filename)

# ── Rank Role Helper ────────────────────────────────────────────────────────

async def assign_rank_role(guild, user_id):
    if not guild: return
    cfg = get_config(); rank_roles = cfg.get('rank_roles', {})
    if not rank_roles: return
    data, uid = get_user(user_id)
    total_wagered = data[uid]['stats']['total_wagered']
    current_rank, _ = get_rank_info(total_wagered)
    rkey = rank_key(current_rank[1])
    role_id = rank_roles.get(rkey)
    member = guild.get_member(user_id)
    if not member: return
    all_rank_ids = set(int(rid) for rid in rank_roles.values())
    to_remove = [r for r in member.roles if r.id in all_rank_ids]
    if to_remove:
        try: await member.remove_roles(*to_remove)
        except: pass
    if role_id:
        role = guild.get_role(int(role_id))
        if role:
            try: await member.add_roles(role)
            except: pass

# ── Provably Fair ─────────────────────────────────────────────────────────

def generate_seeds():
    server_seed = secrets.token_hex(32)
    client_seed = secrets.token_hex(8)
    public_hash = hashlib.sha256(server_seed.encode()).hexdigest()
    return server_seed, client_seed, public_hash

def pf_mine_positions(server_seed, client_seed, mines_count, total=20):
    h = hmac.new(server_seed.encode(), client_seed.encode(), hashlib.sha256)
    rng_bytes = bytes.fromhex(h.hexdigest()); positions = list(range(total))
    for i in range(total - 1, 0, -1):
        j = rng_bytes[i % len(rng_bytes)] % (i + 1)
        positions[i], positions[j] = positions[j], positions[i]
    return set(positions[:mines_count])

def pf_derive(server_seed, client_seed, nonce=0):
    """Return a float [0, 1) derived from seeds + nonce via HMAC-SHA256."""
    msg = f"{client_seed}:{nonce}".encode()
    h = hmac.new(server_seed.encode(), msg, hashlib.sha256)
    return int(h.hexdigest()[:8], 16) / 0xFFFFFFFF

def pf_coinflip(server_seed, client_seed):
    return "heads" if pf_derive(server_seed, client_seed) < 0.5 else "tails"

def pf_dice_roll(server_seed, client_seed):
    return int(pf_derive(server_seed, client_seed) * 6) + 1

def pf_roulette_spin(server_seed, client_seed):
    return int(pf_derive(server_seed, client_seed) * 37)

def pf_slots_spin(server_seed, client_seed):
    symbols = ["🍎", "🍊", "🍋", "🍌", "⭐", "💎"]
    return [symbols[int(pf_derive(server_seed, client_seed, i) * 6)] for i in range(3)]

def pf_blackjack_deck(server_seed, client_seed):
    deck = [2,3,4,5,6,7,8,9,10,10,10,10,11] * 4
    full_bytes = b''
    for i in range(12):
        msg = f"{client_seed}:{i}".encode()
        full_bytes += bytes.fromhex(hmac.new(server_seed.encode(), msg, hashlib.sha256).hexdigest())
    for i in range(len(deck) - 1, 0, -1):
        j = full_bytes[i % len(full_bytes)] % (i + 1)
        deck[i], deck[j] = deck[j], deck[i]
    return deck

def pf_add_field(embed, server_seed, client_seed, public_hash, game):
    """Append a Provably Fair verification field to an embed."""
    embed.add_field(
        name="🔐 Provably Fair",
        value=(
            f"**Server Seed:** `{server_seed}`\n"
            f"**Client Seed:** `{client_seed}`\n"
            f"**Hash (SHA-256):** `{public_hash[:24]}…`\n"
            f"Verify: `.verify {game} {server_seed} {client_seed}`"
        ),
        inline=False
    )


GAME_EMOJIS = {
    'coinflip': '🪙', 'dice': '🎲', 'slots': '🎰', 'roulette': '🎡',
    'blackjack': '🃏', 'mines': '⛏️', 'crash': '🚀', 'jackpot': '🎰',
}

async def send_to_history(guild, game, user_name, user_id, bet, won, profit, new_bal):
    """Post a compact bet result to the configured history channel."""
    if not guild:
        return
    cfg = get_config()
    ch_id = cfg.get('history_channel')
    if not ch_id:
        return
    channel = guild.get_channel(int(ch_id))
    if not channel:
        return
    emoji = GAME_EMOJIS.get(game, '🎮')
    color = 0x00FF88 if won else (0xFFD700 if won is None else 0xFF4444)
    if won is True:
        result_str = f"✅ **WIN** `+R${profit:,}`"
    elif won is False:
        result_str = f"❌ **LOSS** `-R${abs(profit):,}`"
    else:
        result_str = "🤝 **TIE** `no change`"
    embed = discord.Embed(color=color, timestamp=datetime.now(timezone.utc))
    embed.set_author(name=f"{emoji} {game.title()}  ·  {user_name}")
    embed.description = f"**Bet:** R${bet:,}  ·  {result_str}\n**Balance:** R${new_bal:,}"
    try:
        await channel.send(embed=embed)
    except Exception:
        pass

# ── Crash Game ──────────────────────────────────────────────────────────

CRASH_LOBBY_SECS = 20
CRASH_TICK       = 1.0   # seconds between multiplier updates

crash_state = {
    'phase':    'idle',   # idle | lobby | running | crashed
    'bets':     {},       # uid -> {'amount': int, 'start_bal': int, 'username': str}
    'cashed':   {},       # uid -> {'mult': float, 'profit': int}
    'crash_at': 1.0,
    'mult':     1.0,
    'message':  None,
    'channel_id': None,
    'task':     None,
    'view':     None,
    'guild_id': None,
}

def gen_crash_point():
    r = random.random()
    if r < 0.01: return 1.0  # 1% instant crash
    return min(round(0.99 / (1 - r), 2), 200.0)

def crash_mult_at(elapsed):
    return round(1.0 + elapsed * 0.12 + (elapsed ** 1.6) * 0.015, 2)

def crash_embed_build(phase, bets, cashed, mult=1.00, crash_at=None, color=0x1E90FF):
    if phase == 'lobby':
        title = "🚀  Crash — Lobby Open"
        desc  = f"Game starts in a moment!\nUse `.crash <amount>` to bet now.\n\n"
        color = 0x9B59B6
    elif phase == 'running':
        title = f"🚀  Crash — {mult:.2f}×  FLYING"
        desc  = f"**Current Multiplier:** `{mult:.2f}×`\nClick **Cash Out** before it crashes!\n\n"
        color = 0x00FF88 if mult < 3 else (0xFFD700 if mult < 7 else 0xFF5000)
    elif phase == 'crashed':
        title = f"💥  Crashed at {crash_at:.2f}×"
        desc  = f"**Crash Point:** `{crash_at:.2f}×`\n\n"
        color = 0xFF4444
    else:
        title = "🚀  Crash"; desc = ""; color = 0x1E90FF

    if bets:
        lines = []
        for uid, b in bets.items():
            if uid in cashed:
                c = cashed[uid]; sign = "+" if c['profit'] >= 0 else ""
                lines.append(f"✅ **{b['username']}** — cashed {c['mult']:.2f}× ({sign}R${c['profit']:,})")
            elif phase == 'crashed':
                lines.append(f"💥 **{b['username']}** — lost R${b['amount']:,}")
            else:
                lines.append(f"🎲 **{b['username']}** — R${b['amount']:,}")
        desc += "\n".join(lines)

    embed = discord.Embed(title=title, description=desc, color=color)
    if phase == 'lobby':
        embed.set_footer(text=f"Game starts in ~{CRASH_LOBBY_SECS}s after first bet")
    return embed


class CrashView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="💰 Cash Out", style=discord.ButtonStyle.success, custom_id="crash_co")
    async def cashout(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if crash_state['phase'] != 'running':
            await interaction.response.send_message("No active crash game right now!", ephemeral=True); return
        if uid not in crash_state['bets']:
            await interaction.response.send_message("You didn't bet this round! Use `.crash <amount>` next time.", ephemeral=True); return
        if uid in crash_state['cashed']:
            await interaction.response.send_message("You already cashed out!", ephemeral=True); return
        mult    = crash_state['mult']
        bet     = crash_state['bets'][uid]['amount']
        sb      = crash_state['bets'][uid]['start_bal']
        profit  = round(bet * mult) - bet
        new_bal = sb + profit
        set_user_balance(uid, new_bal)
        add_to_stats(uid, True, bet)
        if crash_state['guild_id']:
            guild = bot.get_guild(crash_state['guild_id'])
            if guild:
                asyncio.create_task(assign_rank_role(guild, uid))
        crash_state['cashed'][uid] = {'mult': mult, 'profit': profit}
        await interaction.response.send_message(
            f"✅ Cashed out at **{mult:.2f}×** — profit: **+R${profit:,}**  |  New balance: {fmt(new_bal)}",
            ephemeral=True
        )
        uname = crash_state['bets'][uid].get('username', str(uid))
        guild  = bot.get_guild(crash_state['guild_id']) if crash_state['guild_id'] else None
        asyncio.create_task(send_to_history(guild, 'crash', uname, uid, bet, True, profit, new_bal))


async def run_crash_game(channel, guild_id):
    crash_state['guild_id'] = guild_id
    view = CrashView()
    crash_state['view'] = view

    # Lobby phase
    embed = crash_embed_build('lobby', crash_state['bets'], crash_state['cashed'])
    crash_state['message'] = await channel.send(embed=embed, view=view)

    await asyncio.sleep(CRASH_LOBBY_SECS)

    if not crash_state['bets']:
        crash_state['phase'] = 'idle'
        await crash_state['message'].edit(
            embed=discord.Embed(title="🚀 Crash — Cancelled", description="No bets placed.", color=0x888888),
            view=None)
        return

    # Running phase
    crash_state['phase']    = 'running'
    crash_state['crash_at'] = gen_crash_point()
    crash_state['mult']     = 1.00
    start_time = asyncio.get_event_loop().time()

    while True:
        elapsed = asyncio.get_event_loop().time() - start_time
        crash_state['mult'] = crash_mult_at(elapsed)

        if crash_state['mult'] >= crash_state['crash_at']:
            crash_state['mult'] = crash_state['crash_at']
            break

        embed = crash_embed_build('running', crash_state['bets'], crash_state['cashed'], crash_state['mult'])
        try:
            await crash_state['message'].edit(embed=embed, view=view)
        except Exception:
            pass
        await asyncio.sleep(CRASH_TICK)

    # Crashed
    crash_state['phase'] = 'crashed'
    for uid, b in crash_state['bets'].items():
        if uid not in crash_state['cashed']:
            new_bal = b['start_bal'] - b['amount']
            set_user_balance(uid, max(0, new_bal))
            add_to_stats(uid, False, b['amount'])

    embed = crash_embed_build('crashed', crash_state['bets'], crash_state['cashed'],
                              crash_at=crash_state['crash_at'])
    for item in view.children: item.disabled = True
    try:
        await crash_state['message'].edit(embed=embed, view=view)
    except Exception:
        pass

    await asyncio.sleep(8)

    # Reset
    crash_state.update({'phase': 'idle', 'bets': {}, 'cashed': {}, 'crash_at': 1.0,
                        'mult': 1.0, 'message': None, 'channel_id': None, 'task': None,
                        'view': None, 'guild_id': None})


@bot.command(name='crash')
async def crash_cmd(ctx, amount: str):
    bal = get_user_balance(ctx.author.id)
    amount = resolve_bet(amount, bal)
    if amount is None: await ctx.send("❌ Invalid amount! Use a number, `all`, or `half`."); return
    if amount <= 0: await ctx.send("❌ Bet must be positive!"); return
    if amount > bal: await ctx.send(f"❌ Insufficient balance! You have {fmt(bal)}"); return

    uid = ctx.author.id

    if crash_state['phase'] == 'idle':
        # Start lobby
        crash_state['phase']      = 'lobby'
        crash_state['channel_id'] = ctx.channel.id
        crash_state['bets'][uid]  = {'amount': amount, 'start_bal': bal, 'username': ctx.author.name}
        crash_state['task']       = asyncio.create_task(run_crash_game(ctx.channel, ctx.guild.id if ctx.guild else None))
        await ctx.message.delete()

    elif crash_state['phase'] == 'lobby':
        if crash_state['channel_id'] != ctx.channel.id:
            await ctx.send("❌ A crash game is running in another channel!", delete_after=5); return
        if uid in crash_state['bets']:
            await ctx.send("❌ You already bet this round!", delete_after=5); return
        crash_state['bets'][uid] = {'amount': amount, 'start_bal': bal, 'username': ctx.author.name}
        await ctx.message.delete()
        embed = crash_embed_build('lobby', crash_state['bets'], crash_state['cashed'])
        try: await crash_state['message'].edit(embed=embed, view=crash_state['view'])
        except: pass

    elif crash_state['phase'] == 'running':
        await ctx.send("⏳ A game is already in progress! You can bet on the **next** round.", delete_after=6)
    else:
        await ctx.send("⏳ Please wait — wrapping up the last round.", delete_after=5)

# ── Blackjack ──────────────────────────────────────────────────────────

def cv(cards):
    t = sum(cards); a = cards.count(11)
    while t > 21 and a: t -= 10; a -= 1
    return t

def cs(cards):
    return "  ".join("A" if c == 11 else str(c) for c in cards)

def bj_embed(player_cards, dealer_cards, bet, show_dealer=False,
             title="🃏  Blackjack", color=0x1E90FF, extra=""):
    pv = cv(player_cards); dv = cv(dealer_cards)
    desc = (
        f"**Your hand:** {cs(player_cards)}  —  **{pv}**\n"
        f"**Dealer:** {cs(dealer_cards) + '  — **' + str(dv) + '**' if show_dealer else str(dealer_cards[0]) + '  🂠'}\n\n"
        f"**Bet:** R${bet:,}"
    )
    if extra: desc += f"\n\n{extra}"
    return discord.Embed(title=title, description=desc, color=color)


class BlackjackView(discord.ui.View):
    def __init__(self, user_id, user_name, bet, start_balance, player_cards, dealer_cards, deck):
        super().__init__(timeout=120)
        self.user_id       = user_id; self.user_name = user_name; self.bet = bet
        self.start_balance = start_balance
        self.player_cards  = player_cards; self.dealer_cards = dealer_cards
        self.deck          = deck; self.game_over = False; self.first_action = True
        hit = discord.ui.Button(label="👊 Hit",         style=discord.ButtonStyle.primary,  custom_id="bj_hit")
        std = discord.ui.Button(label="🛑 Stand",       style=discord.ButtonStyle.danger,    custom_id="bj_stand")
        dbl = discord.ui.Button(label="⬆️ Double Down", style=discord.ButtonStyle.secondary, custom_id="bj_double")
        hit.callback = self.hit_callback; std.callback = self.stand_callback; dbl.callback = self.double_callback
        self.add_item(hit); self.add_item(std); self.add_item(dbl)

    def _disable_all(self):
        for item in self.children: item.disabled = True

    def _disable_double(self):
        for item in self.children:
            if getattr(item, 'custom_id', None) == 'bj_double': item.disabled = True

    async def hit_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your game!", ephemeral=True); return
        if self.game_over: return
        self.first_action = False; self._disable_double()
        self.player_cards.append(self.deck.pop())
        if cv(self.player_cards) > 21: await self._finish(interaction, bust=True)
        else: await interaction.response.edit_message(embed=bj_embed(self.player_cards, self.dealer_cards, self.bet), view=self)

    async def stand_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your game!", ephemeral=True); return
        if self.game_over: return
        await self._finish(interaction)

    async def double_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your game!", ephemeral=True); return
        if self.game_over: return
        if not self.first_action:
            await interaction.response.send_message("Can only double on first action!", ephemeral=True); return
        if self.start_balance < self.bet:
            await interaction.response.send_message("Insufficient balance to double!", ephemeral=True); return
        self.first_action = False; self._disable_double()
        self.bet *= 2
        self.player_cards.append(self.deck.pop())
        if cv(self.player_cards) > 21: await self._finish(interaction, bust=True)
        else: await interaction.response.edit_message(embed=bj_embed(self.player_cards, self.dealer_cards, self.bet), view=self)

    async def _finish(self, interaction: discord.Interaction, bust=False):
        self.game_over = True; self._disable_all()
        pv = cv(self.player_cards)
        if bust:
            result_str = "💥 **BUST!** You went over 21."
            profit = -self.bet
            won = False
        else:
            while cv(self.dealer_cards) < 17:
                self.dealer_cards.append(self.deck.pop())
            dv = cv(self.dealer_cards)
            if dv > 21:
                result_str = f"✅ **DEALER BUST!** You win!"
                profit = self.bet
                won = True
            elif pv > dv:
                result_str = f"✅ **YOU WIN!** {pv} vs {dv}"
                profit = self.bet
                won = True
            elif pv < dv:
                result_str = f"❌ **DEALER WINS!** {pv} vs {dv}"
                profit = -self.bet
                won = False
            else:
                result_str = f"🤝 **PUSH!** Both {pv}"
                profit = 0
                won = None

        new_bal = self.start_balance + profit
        set_user_balance(self.user_id, new_bal)
        add_to_stats(self.user_id, won, self.bet)

        extra = f"**Profit:** {'+' if profit >= 0 else ''}{profit:,}\n**New Balance:** {fmt(new_bal)}"
        embed = bj_embed(self.player_cards, self.dealer_cards, self.bet, show_dealer=True, extra=extra, color=0x00FF88 if profit > 0 else 0xFF4444)
        embed.title = "🃏  Blackjack — Game Over"
        embed.description = result_str + "\n\n" + embed.description
        await interaction.response.edit_message(embed=embed, view=self)

@bot.event
async def on_ready():
    print(f"✅ Bot is ready! Logged in as {bot.user}")

@bot.command(name='balance')
async def balance(ctx):
    bal = get_user_balance(ctx.author.id)
    embed = discord.Embed(title="💰 Your Balance", description=fmt(bal), color=0x00FF88)
    await ctx.send(embed=embed)

@bot.command(name='addbal')
@commands.has_permissions(administrator=True)
async def addbal(ctx, user: discord.User, amount: int):
    if amount <= 0:
        await ctx.send("❌ Amount must be positive!"); return
    old_bal = get_user_balance(user.id)
    set_user_balance(user.id, old_bal + amount)
    new_bal = get_user_balance(user.id)
    embed = discord.Embed(title="✅ Balance Added", color=0x00FF88)
    embed.add_field(name="User", value=user.mention, inline=False)
    embed.add_field(name="Amount Added", value=fmt(amount), inline=False)
    embed.add_field(name="New Balance", value=fmt(new_bal), inline=False)
    await ctx.send(embed=embed)

# Bot token
TOKEN = os.getenv('DISCORD_TOKEN', '')
if TOKEN:
    bot.run(TOKEN)
else:
    print("❌ DISCORD_TOKEN not found in environment variables!")
