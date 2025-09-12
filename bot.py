from __future__ import annotations
import os, time
from typing import Dict, List, Optional
from pathlib import Path

from pydantic import BaseModel, Field
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# =================== Persistência ===================
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

MARKETS = {"dragon", "tiger", "tie"}

class Bet(BaseModel):
    ts: float = Field(default_factory=lambda: time.time())
    stake: float
    market: str
    outcome: Optional[str] = None  # win/lose/push
    pnl: Optional[float] = None

class Profile(BaseModel):
    bankroll: float = 0.0
    session_start: float = Field(default_factory=lambda: time.time())
    stop_loss: float = 0.0
    stop_win: float = 0.0
    cooldown_min: int = 0
    last_bet_ts: float = 0.0
    bets: List[Bet] = Field(default_factory=list)
    lifetime_bets: int = 0
    lifetime_pnl: float = 0.0
    probs: Dict[str, float] = Field(default_factory=lambda: {"dragon": 0.5, "tiger": 0.5, "tie": 0.08})
    # auto
    auto_enabled: bool = False
    auto_interval_min: int = 15

    def save(self, uid: int):
        (DATA_DIR / f"{uid}.json").write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @staticmethod
    def load(uid: int) -> "Profile":
        p = DATA_DIR / f"{uid}.json"
        if p.exists():
            return Profile.model_validate_json(p.read_text(encoding="utf-8"))
        return Profile()

# =================== Helpers ===================
def fmt(x: float) -> str:
    return f"€{x:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")

def now() -> float: return time.time()
def session_pnl(p: Profile) -> float: return sum(b.pnl for b in p.bets if b.pnl is not None)

def ensure_cooldown(p: Profile) -> Optional[int]:
    if p.cooldown_min <= 0: return None
    rem = int(p.last_bet_ts + p.cooldown_min*60 - now())
    return rem if rem > 0 else None

def kelly_fraction(p: float, b: float = 1.0) -> float:
    # Kelly para payout 1:1
    q = 1 - p
    f = (b*p - q) / b
    return max(0.0, min(1.0, f))

def advisor_suggestion(p: Profile) -> str:
    # usa apenas probabilidades definidas pelo utilizador
    m_best, prob = max([(m, p.probs.get(m,0.5)) for m in ("dragon","tiger")], key=lambda x: x[1])
    f = kelly_fraction(prob, b=1.0)
    if prob <= 0.5 or f <= 0:
        return "📉 Sem vantagem (>50%). Recomendação: **não apostar agora**."
    stake = max(1.0, round(p.bankroll * f, 2))
    return f"✅ Melhor: **{m_best}** (p={prob:.3f}). Sugestão: stake ~ {fmt(stake)}. Use /cooldown e /setlimits."

# =================== Handlers ===================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = Profile.load(uid); prof.save(uid)
    await update.message.reply_text("✅ Bot ativo! Usa /help para ver comandos.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Comandos:\n"
        "/setbankroll <valor>\n"
        "/setlimits <stop_loss> <stop_win>\n"
        "/cooldown <min>\n"
        "/setprob <mercado> <p 0-1>\n"
        "/prob\n"
        "/suggest\n"
        "/bet <stake> <dragon|tiger|tie>\n"
        "/result <win|lose|push>\n"
        "/stats\n"
        "/reset\n"
        "/auto_on <min>  — envia sugestões automáticas a cada X minutos\n"
        "/auto_off       — desativa envio automático"
    )

async def setbankroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; p = Profile.load(uid)
    try:
        v = float(context.args[0]); assert v>0
    except Exception:
        await update.message.reply_text("Uso: /setbankroll <valor positivo>")
        return
    p.bankroll = v; p.save(uid)
    await update.message.reply_text(f"Banca definida: {fmt(v)}")

async def setlimits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; p = Profile.load(uid)
    if len(context.args)!=2:
        await update.message.reply_text("Uso: /setlimits <stop_loss> <stop_win>")
        return
    try:
        sl = float(context.args[0]); sw = float(context.args[1]); assert sl>=0 and sw>=0
    except Exception:
        await update.message.reply_text("Valores inválidos.")
        return
    p.stop_loss, p.stop_win = sl, sw; p.save(uid)
    await update.message.reply_text(f"Limites: SL {fmt(sl)} | TP {fmt(sw)}")

async def cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; p = Profile.load(uid)
    if not context.args:
        await update.message.reply_text(f"Cooldown atual: {p.cooldown_min} min. Use /cooldown <min>.")
        return
    try:
        m = int(context.args[0]); assert m>=0
    except Exception:
        await update.message.reply_text("Indique minutos ≥ 0.")
        return
    p.cooldown_min = m; p.save(uid)
    await update.message.reply_text(f"Cooldown definido: {m} min.")

async def setprob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; p = Profile.load(uid)
    if len(context.args)!=2:
        await update.message.reply_text("Uso: /setprob <dragon|tiger|tie> <prob 0-1>")
        return
    market = context.args[0].lower()
    try:
        prob = float(context.args[1]); assert 0<=prob<=1
    except Exception:
        await update.message.reply_text("Prob inválida. Use 0..1")
        return
    if market not in MARKETS:
        await update.message.reply_text("Mercado inválido.")
        return
    p.probs[market] = prob; p.save(uid)
    await update.message.reply_text(f"Prob {market} = {prob:.3f}")

async def prob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; p = Profile.load(uid); d = p.probs
    await update.message.reply_text(
        f"Probabilidades:\nDragon {d['dragon']:.3f}\nTiger {d['tiger']:.3f}\nTie {d['tie']:.3f}"
    )

async def suggest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; p = Profile.load(uid)
    rem = ensure_cooldown(p); pnl_s = session_pnl(p)
    if p.stop_loss and pnl_s <= -abs(p.stop_loss):
        await update.message.reply_text("🛑 Stop-loss atingido.")
        return
    if p.stop_win and pnl_s >= abs(p.stop_win):
        await update.message.reply_text("✅ Objetivo de lucro atingido.")
        return
    if rem:
        await update.message.reply_text(f"⏳ Cooldown: {rem}s.")
        return
    await update.message.reply_text(advisor_suggestion(p))

async def bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; p = Profile.load(uid)
    if len(context.args)!=2:
        await update.message.reply_text("Uso: /bet <stake> <mercado>")
        return
    try:
        stake = float(context.args[0]); market = context.args[1].lower()
        assert stake>0 and market in MARKETS
    except Exception:
        await update.message.reply_text("Parâmetros inválidos.")
        return
    rem = ensure_cooldown(p)
    if rem:
        await update.message.reply_text(f"⏳ Aguarde {rem}s.")
        return
    p.bets.append(Bet(stake=stake, market=market))
    p.last_bet_ts = time.time(); p.save(uid)
    await update.message.reply_text(f"Aposta registada: {fmt(stake)} em {market}.")

async def result_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; p = Profile.load(uid)
    if not context.args:
        await update.message.reply_text("Uso: /result <win|lose|push>")
        return
    outcome = context.args[0].lower()
    if outcome not in {"win","lose","push"}:
        await update.message.reply_text("Resultado inválido.")
        return
    open_bet = next((b for b in reversed(p.bets) if b.outcome is None), None)
    if not open_bet:
        await update.message.reply_text("Não há aposta aberta.")
        return
    open_bet.outcome = outcome
    if outcome=="win": open_bet.pnl = open_bet.stake
    elif outcome=="lose": open_bet.pnl = -open_bet.stake
    else: open_bet.pnl = 0.0
    p.lifetime_bets += 1; p.lifetime_pnl += open_bet.pnl; p.bankroll += open_bet.pnl
    p.save(uid)
    await update.message.reply_text(f"Fechada: {outcome.upper()} | PnL {fmt(open_bet.pnl)} | Banca {fmt(p.bankroll)}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; p = Profile.load(uid)
    bets_done = [b for b in p.bets if b.pnl is not None]
    session_bets = len(bets_done)
    wins = sum(1 for b in bets_done if b.outcome=="win")
    loses = sum(1 for b in bets_done if b.outcome=="lose")
    pushes = sum(1 for b in bets_done if b.outcome=="push")
    pnl_s = sum(b.pnl for b in bets_done) if bets_done else 0.0
    wr = (wins/session_bets*100) if session_bets else 0.0
    await update.message.reply_text(
        f"📊 Sessão: {session_bets} | PnL {fmt(pnl_s)} | WR {wr:.1f}%\n"
        f"W/L/P: {wins}/{loses}/{pushes}\nBanca: {fmt(p.bankroll)} | SL {fmt(p.stop_loss)} | TP {fmt(p.stop_win)} | Cooldown {p.cooldown_min}m\n"
        f"Vida toda: {p.lifetime_bets} | PnL {fmt(p.lifetime_pnl)}"
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; p = Profile.load(uid)
    p.session_start = time.time(); p.bets = []; p.save(uid)
    await update.message.reply_text("Sessão reiniciada.")

# =================== AUTO (JobQueue) ===================
def _cancel_jobs_for(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    # usar job_queue do application
    for j in context.application.job_queue.get_jobs_by_name(f"auto-{chat_id}"):
        j.schedule_removal()

async def auto_tick(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    uid = chat_id  # para chats 1:1 funciona bem
    p = Profile.load(uid)
    rem = ensure_cooldown(p)
    pnl_s = session_pnl(p)
    if p.stop_loss and pnl_s <= -abs(p.stop_loss):
        await context.bot.send_message(chat_id, "🛑 Stop-loss atingido. Auto desligado.")
        _cancel_jobs_for(chat_id, context); p.auto_enabled = False; p.save(uid); return
    if p.stop_win and pnl_s >= abs(p.stop_win):
        await context.bot.send_message(chat_id, "✅ Objetivo de lucro atingido. Auto desligado.")
        _cancel_jobs_for(chat_id, context); p.auto_enabled = False; p.save(uid); return
    if rem:  # respeita cooldown
        return
    await context.bot.send_message(chat_id, advisor_suggestion(p))

async def auto_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    p = Profile.load(uid)
    try:
        minutes = int(context.args[0]) if context.args else p.auto_interval_min
        assert minutes >= 1
    except Exception:
        await update.message.reply_text("Uso: /auto_on <minutos>  (ex.: /auto_on 5)")
        return
    p.auto_enabled = True; p.auto_interval_min = minutes; p.save(uid)
    _cancel_jobs_for(chat_id, context)
    context.application.job_queue.run_repeating(
        auto_tick, interval=minutes*60, first=0, chat_id=chat_id, name=f"auto-{chat_id}"
    )
    await update.message.reply_text(f"🔔 Auto ligado: enviarei sugestões a cada {minutes} min. Use /auto_off para parar.")

async def auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    _cancel_jobs_for(chat_id, context)
    p = Profile.load(uid); p.auto_enabled = False; p.save(uid)
    await update.message.reply_text("🔕 Auto desligado.")

async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Não reconheço esse comando. Use /help.")

# =================== Main ===================
def main():
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN não definido. Configure no Render (Environment).")
    app = ApplicationBuilder().token(token).build()

    # Comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setbankroll", setbankroll))
    app.add_handler(CommandHandler("setlimits", setlimits))
    app.add_handler(CommandHandler("cooldown", cooldown))
    app.add_handler(CommandHandler("setprob", setprob))
    app.add_handler(CommandHandler("prob", prob))
    app.add_handler(CommandHandler("suggest", suggest))
    app.add_handler(CommandHandler("bet", bet))
    app.add_handler(CommandHandler("result", result_cmd))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("auto_on", auto_on))
    app.add_handler(CommandHandler("auto_off", auto_off))
    app.add_handler(MessageHandler(filters.COMMAND, fallback))

    print("Bot a correr (Render) com auto_on/auto_off.")
    app.run_polling(close_loop=False, drop_pending_updates=True)

if __name__ == "__main__":
    main()
