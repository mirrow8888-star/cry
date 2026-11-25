# ═══ Google 試算表 → Telegram 查詢投放系統 · Streamlit 網頁版 ═══
# 執行：streamlit run gsheet_telegram_web.py
# 流程：輸入 Sheet ID / gid → 載入資料 → 勾選欄位 → 設定前 N 筆 → 預覽 → 投放到 Telegram
# 注意：試算表需在 Google 試算表「共用」設為「知道連結的任何人」可檢視。
from __future__ import annotations

import html
import io
import re
import time
from datetime import datetime

import httpx
import pandas as pd
import streamlit as st

TG_HOST = "https://api.telegram.org"
MSG_BUDGET = 3800          # Telegram 單則上限 4096 字，保留餘量給標頭與標籤
VALUE_MAX = 300            # 單一欄位值最多顯示字數


# ═══════════════ Google 試算表 ═══════════════
class SheetError(Exception):
    pass


_SHEET_RE = re.compile(r"/d/([a-zA-Z0-9_-]+)")
_GID_RE = re.compile(r"[#&?]gid=(\d+)")


def parse_sheet_ref(sheet_text: str, gid_text: str) -> tuple[str, str]:
    """接受 Sheet ID 或完整網址；gid 留空時嘗試從網址取得，否則用 0。"""
    s, gid = sheet_text.strip(), gid_text.strip()
    m = _SHEET_RE.search(s)
    sid = m.group(1) if m else s
    if not gid:
        g = _GID_RE.search(s)
        gid = g.group(1) if g else "0"
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", sid):
        raise ValueError("Sheet ID 格式不正確（請貼上網址 /d/ 與 /edit 之間那一段，或直接貼完整網址）")
    if not gid.isdigit():
        raise ValueError("gid 必須是數字（網址最後 #gid= 後面的數字）")
    return sid, gid


def read_gsheet(sid: str, gid: str) -> pd.DataFrame:
    """以 CSV 匯出方式讀取公開的 Google 試算表；全部以文字讀入，保留資料原樣（不會把 001 變 1、空白變 NaN）。"""
    url = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"
    r = httpx.get(url, follow_redirects=True, timeout=30)
    if r.status_code == 404:
        raise SheetError("找不到試算表或分頁，請檢查 Sheet ID 與 gid")
    if r.status_code != 200 or "csv" not in r.headers.get("content-type", "").lower():
        raise SheetError("無法讀取：請把試算表「共用」設為「知道連結的任何人」可檢視")
    df = pd.read_csv(io.BytesIO(r.content), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if df.empty or len(df.columns) == 0:
        raise SheetError("這個分頁沒有資料")
    df.columns = [f"欄位{i + 1}" if (str(c).startswith("Unnamed:") or not str(c).strip()) else str(c).strip()
                  for i, c in enumerate(df.columns)]
    return df


# ═══════════════ Telegram ═══════════════
class TgError(Exception):
    def __init__(self, code, desc):
        super().__init__(f"{code} {desc}")
        self.code, self.desc = code, desc


def tg_api(token, method, **payload):
    r = httpx.post(f"{TG_HOST}/bot{token}/{method}", json=payload, timeout=20)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or not data.get("ok"):
        raise TgError(data.get("error_code", r.status_code), data.get("description", r.reason_phrase))
    return data.get("result")


def tg_check(token, chat_id=""):
    me = tg_api(token, "getMe")
    who = "@" + me["username"] if me.get("username") else me.get("first_name", "Telegram Bot")
    if chat_id:
        tg_api(token, "getChat", chat_id=chat_id)
    else:
        who += "（尚未填 Chat ID）"
    return who


def tg_find_chat(token):
    """從 getUpdates 取得最近一位傳訊息給 Bot 的 Chat ID。"""
    for u in reversed(tg_api(token, "getUpdates", limit=100)):
        m = u.get("message") or u.get("edited_message") or u.get("channel_post") or u.get("my_chat_member")
        if m and "chat" in m:
            c = m["chat"]
            return str(c["id"]), c.get("title") or c.get("first_name") or c.get("username") or ""
    raise ValueError("尚未收到訊息：請先在 Telegram 對你的 Bot 傳送任意訊息（例如 /start），再按一次")


def tg_send_html(token, chat_id, text):
    tg_api(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)


def tg_err(e):
    if isinstance(e, (ValueError, SheetError)):
        return str(e)
    if isinstance(e, TgError):
        d = e.desc.lower()
        if "chat not found" in d:
            return "找不到此 Chat ID（請先在 Telegram 對 Bot 按 /start，或確認 ID 正確）"
        hint = {400: "請求格式錯誤", 401: "Bot Token 無效", 403: "Bot 被封鎖或無權限發言", 404: "Bot Token 格式不正確",
                409: "Bot 已設定 Webhook，無法自動取得 Chat ID，請手動輸入", 429: "請求過於頻繁，請稍後再試"}.get(e.code, "")
        return f"HTTP {e.code} {hint or e.desc}".strip()
    if isinstance(e, httpx.HTTPError):
        return f"網路連線失敗（{type(e).__name__}）"
    return f"{type(e).__name__}: {e}"


# ═══════════════ 訊息排版（乾淨、有條理、帶 emoji）═══════════════
_EMOJI_RULES = [
    (r"mail|信箱|郵件", "📧"),
    (r"電話|手機|phone|tel|mobile", "📞"),
    (r"日期|時間|date|time|day|年月", "📅"),
    (r"金額|價格|單價|售價|費|薪|收入|成本|price|amount|cost|total|revenue|salary", "💰"),
    (r"地址|地點|縣市|城市|區域|address|location|city|place", "📍"),
    (r"數量|庫存|件數|qty|quantity|count|stock", "📦"),
    (r"狀態|進度|status|state|progress", "🚦"),
    (r"備註|說明|描述|內容|note|remark|desc|comment", "📝"),
    (r"網址|連結|url|link|http", "🔗"),
    (r"分數|成績|評分|score|rating|grade", "⭐"),
    (r"品名|產品|商品|product|item", "🛍️"),
    (r"類別|分類|種類|類型|type|category|class", "🏷️"),
    (r"編號|序號|代碼|代號|(^|[^a-z])id($|[^a-z])|code|number|^no\.?$", "🔖"),
    (r"姓名|名稱|名字|客戶|人員|name|user|member|customer", "👤"),
]


def col_emoji(name: str) -> str:
    n = name.lower()
    for pat, e in _EMOJI_RULES:
        if re.search(pat, n):
            return e
    return "🔹"


def _clean(v) -> str:
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s if len(s) <= VALUE_MAX else s[:VALUE_MAX - 1] + "…"


def build_messages(df: pd.DataFrame, cols: list[str], n: int, title: str, hide_empty: bool,
                   html_mode: bool = True) -> list[str]:
    """把前 n 筆、選定欄位排成一或多則訊息（每則 ≤ MSG_BUDGET 字）。
    html_mode=True 給 Telegram（粗體標籤＋跳脫）；False 給本機預覽（純文字）。"""
    esc = (lambda s: html.escape(str(s), quote=False)) if html_mode else str
    bold = (lambda s: f"<b>{esc(s)}</b>") if html_mode else str
    rule = "━━━━━━━━━━━━━━"
    sub = df.head(n)
    title = title.strip() or "Google 試算表查詢結果"

    head = "\n".join([bold(f"📊 {title}"), rule,
                      f"🗂 欄位：{esc('、'.join(cols))}",
                      f"🔢 顯示前 {len(sub)} 筆（共 {len(df)} 筆）",
                      f"🕒 {datetime.now():%Y-%m-%d %H:%M}", rule])
    cont = "\n".join([bold(f"📊 {title}（續）"), rule])

    blocks = []
    for i, (_, row) in enumerate(sub.iterrows(), 1):
        lines = [bold(f"📌 第 {i} 筆")]
        for c in cols:
            v = _clean(row[c])
            if not v:
                if hide_empty:
                    continue
                v = "—"
            lines.append(f"{col_emoji(c)} {esc(c)}：{esc(v)}")
        while len("\n".join(lines)) > MSG_BUDGET - len(cont) - 40 and len(lines) > 2:   # 極端情況：欄位太多
            lines.pop()
            lines[-1] = "…（內容過長已截斷）"
        blocks.append("\n".join(lines))

    parts, cur, has_block = [], head, False
    for blk in blocks:
        if has_block and len(cur) + 2 + len(blk) > MSG_BUDGET:
            parts.append(cur)
            cur, has_block = cont, False
        cur += "\n\n" + blk
        has_block = True
    foot = f"{rule}\n✅ 以上共 {len(sub)} 筆"
    if len(cur) + 2 + len(foot) > MSG_BUDGET:
        parts.append(cur)
        cur = foot
    else:
        cur += "\n\n" + foot
    parts.append(cur)

    if len(parts) > 1:
        parts = [f"📄 第 {i}/{len(parts)} 則\n{p}" for i, p in enumerate(parts, 1)]
    return parts


# ═══════════════ 網頁介面 ═══════════════
st.set_page_config(page_title="試算表 → Telegram", page_icon="📊", layout="wide")


def init_state():
    S = st.session_state
    S.setdefault("df", None)
    S.setdefault("n_rows", 10)
    S.setdefault("msgs", {"sheet": ("", ""), "tg": ("", "")})   # 側欄狀態 (ok/err, 文字)


def show_msg(key, hint=""):
    kind, text = st.session_state.msgs[key]
    if text:
        (st.success if kind == "ok" else st.error)(text)
    elif hint:
        st.caption(hint)


# ── 按鈕 callback（在頁面重新繪製前執行，可安全修改 widget 的 session_state）──
def cb_all(state: bool):
    st.session_state.cols_sel = list(st.session_state.df.columns) if state else []


def cb_invert():
    S = st.session_state
    S.cols_sel = [c for c in S.df.columns if c not in S.cols_sel]


def cb_find_chat():
    S = st.session_state
    tok = S.get("tg_tok", "").strip()
    if not tok:
        S.msgs["tg"] = ("err", "⚠ 請先貼上 Bot Token")
        return
    try:
        cid, name = tg_find_chat(tok)
        S.tg_chat = cid
        S.msgs["tg"] = ("ok", f"✓ 已取得 Chat ID：{cid}" + (f"（{name}）" if name else ""))
    except Exception as e:  # noqa: BLE001
        S.msgs["tg"] = ("err", f"✗ {tg_err(e)}")


def cb_verify():
    S = st.session_state
    tok, chat = S.get("tg_tok", "").strip(), S.get("tg_chat", "").strip()
    if not tok:
        S.msgs["tg"] = ("err", "⚠ 請先貼上 Bot Token")
        return
    try:
        S.msgs["tg"] = ("ok", f"✓ 驗證成功 · {tg_check(tok, chat)}")
    except Exception as e:  # noqa: BLE001
        S.msgs["tg"] = ("err", f"✗ 驗證失敗：{tg_err(e)}")


def cb_load():
    S = st.session_state
    try:
        sid, gid = parse_sheet_ref(S.get("sheet_in", ""), S.get("gid_in", ""))
        df = read_gsheet(sid, gid)
    except Exception as e:  # noqa: BLE001
        S.msgs["sheet"] = ("err", f"✗ {tg_err(e)}")
        return
    S.df = df
    S.cols_sel = list(df.columns)            # 載入後預設全選
    S.n_rows = min(10, len(df))
    S.msgs["sheet"] = ("ok", f"✓ 已載入 {len(df)} 筆 × {len(df.columns)} 欄")


def main():
    init_state()
    S = st.session_state

    # ── 側欄 ──
    with st.sidebar:
        st.title("📊 試算表查詢投放")
        with st.container(border=True):
            st.markdown("**📗 Google 試算表**")
            st.text_input("Sheet ID 或完整網址", key="sheet_in", placeholder="Sheet ID 或完整網址")
            st.text_input("gid（分頁編號，留空 = 0）", key="gid_in", placeholder="0")
            st.button("📥 載入資料", type="primary", on_click=cb_load)
            show_msg("sheet", "需將試算表共用設為「知道連結的任何人」可檢視。")

        with st.container(border=True):
            st.markdown("**✈️ Telegram**")
            st.text_input("Bot Token", key="tg_tok", type="password", placeholder="向 @BotFather 取得")
            st.text_input("Your Chat ID", key="tg_chat", placeholder="例如 123456789")
            c1, c2 = st.columns(2)
            c1.button("🔍 取得 Chat ID", on_click=cb_find_chat)
            c2.button("✔ 驗證", on_click=cb_verify)
            show_msg("tg", "請先對你的 Bot 傳一則訊息（/start），再按「取得 Chat ID」。")

    # ── 主區 ──
    st.header("📊 Google 試算表 → Telegram")
    df = S.df
    if df is None:
        st.info("請在左側輸入 Sheet ID / gid，按「載入資料」開始。")
        return
    st.caption(f"📗 {len(df)} 筆 · {len(df.columns)} 欄")

    with st.container(border=True):
        h1, h2, h3, h4 = st.columns([5, 1, 1, 1])
        h1.markdown("**🧩 選擇要投放的欄位**")
        h2.button("全選", on_click=cb_all, args=(True,))
        h3.button("全不選", on_click=cb_all, args=(False,))
        h4.button("反選", on_click=cb_invert)
        cols = st.multiselect("欄位", list(df.columns), key="cols_sel", label_visibility="collapsed",
                              placeholder="請至少選擇一個欄位")

    with st.container(border=True):
        o1, o2, o3 = st.columns([1, 3, 1.4])
        n = o1.number_input("⚙️ 前 N 筆", min_value=1, max_value=len(df), step=1, key="n_rows")
        title = o2.text_input("訊息標題", value="Google 試算表查詢結果")
        hide_empty = o3.toggle("略過空白欄位", value=True)

    if not cols:
        st.warning("請至少勾選一個欄位。")
        return
    n = int(n)
    preview = build_messages(df, cols, n, title, hide_empty, html_mode=False)
    sending = build_messages(df, cols, n, title, hide_empty, html_mode=True)

    t_data, t_msg = st.tabs(["📋 資料預覽", "✉️ Telegram 訊息預覽"])
    with t_data:
        st.dataframe(df.head(n)[cols], hide_index=True)
    with t_msg:
        for i, m in enumerate(preview):
            if i:
                st.divider()
            st.text(m)

    tok, chat = S.get("tg_tok", "").strip(), S.get("tg_chat", "").strip()
    st.caption(f"將投放 {n} 筆 × {len(cols)} 欄，共 {len(preview)} 則訊息")
    if st.button("📤 投放到 Telegram", type="primary", disabled=not (tok and chat)):
        bar = st.progress(0.0, text="投放中…")
        try:
            for i, m in enumerate(sending, 1):
                tg_send_html(tok, chat, m)
                bar.progress(i / len(sending), text=f"已投放 {i}/{len(sending)} 則")
                if i < len(sending):
                    time.sleep(1.1)   # 避免觸發 Telegram 的頻率限制
            st.success(f"✓ 已投放 {len(sending)} 則訊息 · {datetime.now():%H:%M:%S}")
        except Exception as e:  # noqa: BLE001
            st.error(f"✗ 投放失敗：{tg_err(e)}")
    if not (tok and chat):
        st.caption("⚠ 請先在左側填寫 Telegram Bot Token 與 Chat ID。")


main()
