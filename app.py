from pathlib import Path
from datetime import date, datetime, timezone, timedelta
import json

import streamlit as st
import streamlit.components.v1 as components
import requests
from requests.auth import HTTPBasicAuth

st.set_page_config(
    page_title="BPM Team Project Management Dashboard",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 隱藏 Streamlit 預設的 header/footer，讓 dashboard 滿版呈現
st.markdown(
    """
    <style>
      #MainMenu, header, footer {visibility: hidden;}
      .block-container {padding: 0.6rem 1rem 0 !important; max-width: 100% !important;}
      iframe {display: block; width: 100%; border: none;}
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Jira 連線設定（存在 Streamlit secrets 裡，不要寫死在程式碼）──
# .streamlit/secrets.toml 需要：
#   JIRA_DOMAIN = "alp-bpmteam-dashboard.atlassian.net"
#   JIRA_EMAIL = "harry.yang@alp.global"
#   JIRA_API_TOKEN = "..."   (Jira 帳號設定 -> Security -> API tokens 建立)
JIRA_DOMAIN = st.secrets["JIRA_DOMAIN"]
JIRA_EMAIL = st.secrets["JIRA_EMAIL"]
JIRA_API_TOKEN = st.secrets["JIRA_API_TOKEN"]

JIRA_BASE = f"https://{JIRA_DOMAIN}/rest/api/3"
AUTH = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
HEADERS = {"Accept": "application/json"}

# Jira site 上的範例／示範專案，掃描全部專案時排除
EXCLUDE_PROJECT_KEYS = {"SAM1", "KAN"}

# 自訂欄位對照表（同一個 Jira site，自訂欄位 ID 全站共用，9 個專案都適用）
FIELD_START      = "customfield_10015"  # 開始日期
FIELD_END        = "customfield_10048"  # 結束日期
FIELD_ACTUAL_END = "customfield_10049"  # 實際完成日
FIELD_OWNER      = "customfield_10044"  # 負責人
FIELD_DECIDE     = "customfield_10046"  # 須優先決議
FIELD_NOTE       = "customfield_10043"  # 決議事項說明
FIELD_PROG_NOTE  = "customfield_10045"  # 進度說明
FIELD_PRIORITY   = "customfield_10042"  # 優先順序（自訂文字欄位，非 Jira 內建 priority）

REQUEST_FIELDS = [
    "summary", "status", "priority", FIELD_PRIORITY,
    FIELD_START, FIELD_END, FIELD_ACTUAL_END,
    FIELD_OWNER, FIELD_DECIDE, FIELD_NOTE, FIELD_PROG_NOTE,
]

# ── 更新資料按鈕 ──
col1, _ = st.columns([1, 9])
with col1:
    if st.button("🔄 更新資料"):
        st.cache_data.clear()
        st.rerun()


def _doc_to_text(v):
    """把 Jira 的 ADF (Atlassian Document Format) 段落轉成純文字；
    也相容欄位本身就是純文字字串的情況。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        out = []
        for c in v.get("content", []):
            if c.get("type") == "paragraph":
                for t in c.get("content", []):
                    if t.get("type") == "text":
                        out.append(t.get("text", ""))
            elif c.get("type") == "text":
                out.append(c.get("text", ""))
        return "".join(out)
    return ""


def _to_date(d):
    if not d:
        return None
    try:
        return datetime.fromisoformat(str(d)[:10]).date()
    except ValueError:
        return None


def calc_overdue_days(status: str, end_date, actual_date=None, today=None) -> int:
    """逾期天數，對應原本 Notion 公式的邏輯：
    - 進行中：今天 相對 結束日期，逾期才計數，否則 0
    - 已完成：實際完成日 相對 結束日期，晚於預期才計數，否則 0
    - 其他狀態（未開始等）：0
    """
    today = today or date.today()
    end_d = _to_date(end_date)
    actual_d = _to_date(actual_date)

    if status == "進行中":
        if end_d is None:
            return 0
        diff = (today - end_d).days
        return diff if diff > 0 else 0

    if status == "已完成":
        if actual_d is None or end_d is None:
            return 0
        diff = (actual_d - end_d).days
        return diff if diff > 0 else 0

    return 0


@st.cache_data(ttl=300)
def fetch_projects():
    """列出這個 Jira site 上所有專案，排除範例／示範專案。"""
    res = requests.get(
        f"{JIRA_BASE}/project/search",
        auth=AUTH, headers=HEADERS, params={"maxResults": 100},
    )
    res.raise_for_status()
    values = res.json().get("values", [])
    return [p for p in values if p["key"] not in EXCLUDE_PROJECT_KEYS]


@st.cache_data(ttl=300)
def fetch_all_tasks():
    tasks = []
    errors = []

    for proj in fetch_projects():
        proj_key = proj["key"]
        proj_name = proj["name"]
        try:
            next_page_token = None
            while True:
                params = {
                    "jql": f'project = "{proj_key}" ORDER BY created ASC',
                    "fields": ",".join(REQUEST_FIELDS),
                    "maxResults": 100,
                }
                if next_page_token:
                    params["nextPageToken"] = next_page_token

                # 注意：舊版 /rest/api/3/search 已被 Atlassian 下架（2025年起回傳 410 Gone），
                # 這裡改用新版 /rest/api/3/search/jql，分頁方式也從 startAt/total 換成 nextPageToken/isLast。
                res = requests.get(
                    f"{JIRA_BASE}/search/jql",
                    auth=AUTH, headers=HEADERS, params=params,
                )
                res.raise_for_status()
                data = res.json()
                issues = data.get("issues", [])

                for issue in issues:
                    f = issue["fields"]
                    status = (f.get("status") or {}).get("name") or "未開始"

                    prio_custom = _doc_to_text(f.get(FIELD_PRIORITY))
                    prio_native = (f.get("priority") or {}).get("name", "")
                    prio = prio_custom or prio_native

                    end_d = f.get(FIELD_END)
                    actual_end = f.get(FIELD_ACTUAL_END)

                    progress = 100 if status == "已完成" else (0 if status == "未開始" else 50)

                    tasks.append({
                        "proj": proj_name,
                        "task": f.get("summary", ""),
                        "owner": _doc_to_text(f.get(FIELD_OWNER)),
                        "prio": prio,
                        "status": status,
                        "start": f.get(FIELD_START),
                        "end": end_d,
                        "progress": progress,
                        "decide": _doc_to_text(f.get(FIELD_DECIDE)) or "否",
                        "note": _doc_to_text(f.get(FIELD_NOTE)),
                        "prog_note": _doc_to_text(f.get(FIELD_PROG_NOTE)),
                        "actual_end": actual_end or None,
                        "overdue_days": calc_overdue_days(status, end_d, actual_end),
                    })

                next_page_token = data.get("nextPageToken")
                if data.get("isLast", True) or not next_page_token or not issues:
                    break
        except Exception as e:
            errors.append(f"{proj_name}: {e}")

    return tasks, errors


with st.spinner("從 Jira 載入資料中..."):
    tasks, errors = fetch_all_tasks()

for e in errors:
    st.warning(f"⚠️ {e}")

if not tasks:
    st.error("無法載入任何任務資料，請確認 Jira Token / 網域是否正確、專案是否存在。")
    st.stop()

today_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

tasks_json = json.dumps(tasks, ensure_ascii=False)
# 防護：欄位內容若剛好包含 "</script>"，未跳脫會提前關閉整段 <script>，
# 導致頁面壞掉甚至有 XSS 風險，因此把 "</" 轉成 JS 可安全解析的 "<\/"。
tasks_json = tasks_json.replace("</", "<\\/")

HTML_PATH = Path(__file__).parent / "dashboard.html"
if not HTML_PATH.exists():
    st.error(f"找不到 {HTML_PATH.name}，請確認它和 app.py 放在 repo 同一層。")
    st.stop()

html = HTML_PATH.read_text(encoding="utf-8")
html = html.replace("__SNAPSHOT_DATETIME__", today_str)
html = html.replace("__TASKS_JSON__", tasks_json)
html = html.replace("__GEMINI_API_KEY__", st.secrets.get("GEMINI_API_KEY", ""))

st.html(html)

# ── AD 小幫手 Sidebar（可讀取專案資料）──
import requests as _req, json as _json

with st.sidebar:
    st.image("https://raw.githubusercontent.com/HarryYang-ALP/AD-chatbot/main/logo.png", width=60)
    st.markdown("### AD 小幫手")
    st.caption("可詢問 BPM 問題或專案進度")
    st.divider()

    if "ad_messages" not in st.session_state:
        st.session_state.ad_messages = []
    if "ad_history" not in st.session_state:
        st.session_state.ad_history = []

    for msg in st.session_state.ad_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if not st.session_state.ad_messages:
        st.markdown("**💡 快速提問：**")
        for _q in ["目前有幾個任務逾期？", "哪些任務須優先決議？", "各負責人任務數？", "如何登入 BPM？", "採購單核決權限？"]:
            if st.button(_q, key=f"qq_{_q}", use_container_width=True):
                st.session_state.ad_pending = _q
                st.rerun()

    if "ad_pending" in st.session_state:
        _prompt = st.session_state.pop("ad_pending")
    else:
        _prompt = None

    _user_input = st.chat_input("請輸入你的問題...", key="ad_input")
    if _user_input:
        _prompt = _user_input

    if _prompt:
        st.session_state.ad_messages.append({"role": "user", "content": _prompt})
        st.session_state.ad_history.append({"role": "user", "parts": [{"text": _prompt}]})
        with st.chat_message("user"):
            st.markdown(_prompt)

        _GKEY = st.secrets.get("GEMINI_API_KEY", "")
        _API = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={_GKEY}"

        # 把 tasks 資料摘要傳給 AI
        _tasks_summary = _json.dumps([{
            "專案": t.get("proj",""),
            "任務": t.get("task",""),
            "狀態": t.get("status",""),
            "負責人": t.get("owner",""),
            "進度": str(t.get("progress","")) + "%",
            "結束日": t.get("end",""),
            "逾期天數": t.get("overdue_days", 0),
            "須決議": t.get("decide",""),
            "優先": t.get("prio","")
        } for t in tasks], ensure_ascii=False)

        _SYS = f"""你是ALP公司的AD小幫手，可以回答BPM系統問題，也可以根據以下專案資料回答問題。
請用繁體中文回答，清楚簡潔。資料快照時間：{today_str}

目前專案任務資料：
{_tasks_summary}

BPM知識庫：登入用Azure AD Login / 代理人設定：Personal>Account>Task Rules加Delegation / 採購LOA：3萬以下主管,3萬-30萬採購成控+主管,30萬-500萬+營運長,500萬-3000萬+執行長,3000萬以上+董事長 / 出差3工作天前申請"""

        with st.chat_message("assistant"):
            with st.spinner("思考中..."):
                try:
                    _res = _req.post(_API, json={
                        "system_instruction": {"parts": [{"text": _SYS}]},
                        "contents": st.session_state.ad_history
                    }, timeout=30)
                    _data = _res.json()
                    _reply = _data["candidates"][0]["content"]["parts"][0]["text"]
                except Exception as _e:
                    _reply = f"發生錯誤：{str(_e)}"
                st.markdown(_reply)

        st.session_state.ad_messages.append({"role": "assistant", "content": _reply})
        st.session_state.ad_history.append({"role": "model", "parts": [{"text": _reply}]})
        st.rerun()

# ── AD 小幫手 Sidebar ──
import requests as _req, json as _json

with st.sidebar:
    st.image("https://raw.githubusercontent.com/HarryYang-ALP/AD-chatbot/main/logo.png", width=60)
    st.markdown("### AD 小幫手")
    st.caption("詢問專案進度、任務狀況")
    st.divider()

    if "ad_msg" not in st.session_state:
        st.session_state.ad_msg = []
    if "ad_hist" not in st.session_state:
        st.session_state.ad_hist = []

    for m in st.session_state.ad_msg:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    if prompt := st.chat_input("問我專案進度...", key="ad_input"):
        st.session_state.ad_msg.append({"role":"user","content":prompt})
        st.session_state.ad_hist.append({"role":"user","parts":[{"text":prompt}]})
        with st.chat_message("user"):
            st.markdown(prompt)

        _tasks_json = _json.dumps([{
            "專案":t.get("proj",""),
            "任務":t.get("task",""),
            "狀態":t.get("status",""),
            "負責人":t.get("owner",""),
            "進度":str(t.get("progress",""))+"%",
            "結束日":t.get("end",""),
            "逾期天數":t.get("overdue_days",0),
            "須決議":t.get("decide",""),
            "優先":t.get("prio",""),
            "進度說明":t.get("prog_note","")
        } for t in tasks], ensure_ascii=False)

        _sys = f"""你是 BPM Team 的專案進度助理。
請根據以下 Jira 任務資料，用繁體中文回答使用者的問題。
資料快照時間：{today_str}

任務資料：
{_tasks_json}"""

        with st.chat_message("assistant"):
            with st.spinner("查詢中..."):
                try:
                    _r = _req.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={st.secrets.get('GEMINI_API_KEY','')}",
                        json={"system_instruction":{"parts":[{"text":_sys}]},"contents":st.session_state.ad_hist},
                        timeout=30
                    )
                    _reply = _r.json()["candidates"][0]["content"]["parts"][0]["text"]
                except Exception as e:
                    _reply = f"錯誤：{e}"
                st.markdown(_reply)

        st.session_state.ad_msg.append({"role":"assistant","content":_reply})
        st.session_state.ad_hist.append({"role":"model","parts":[{"text":_reply}]})
