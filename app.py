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
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
  #MainMenu, header, footer {visibility: hidden;}
  .block-container {padding: 0.6rem 1rem 0 !important; max-width: 100% !important;}
  iframe {display: block; width: 100%; border: none;}
</style>
""", unsafe_allow_html=True)

JIRA_DOMAIN = st.secrets["JIRA_DOMAIN"]
JIRA_EMAIL = st.secrets["JIRA_EMAIL"]
JIRA_API_TOKEN = st.secrets["JIRA_API_TOKEN"]
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

JIRA_BASE = f"https://{JIRA_DOMAIN}/rest/api/3"
AUTH = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

EXCLUDE_PROJECT_KEYS = {"SAM1", "KAN"}

FIELD_START      = "customfield_10015"
FIELD_END        = "customfield_10048"
FIELD_ACTUAL_END = "customfield_10049"
FIELD_OWNER      = "customfield_10044"
FIELD_DECIDE     = "customfield_10046"
FIELD_NOTE       = "customfield_10043"
FIELD_PROG_NOTE  = "customfield_10045"
FIELD_PRIORITY   = "customfield_10042"

REQUEST_FIELDS = [
    "summary", "status", "priority", FIELD_PRIORITY,
    FIELD_START, FIELD_END, FIELD_ACTUAL_END,
    FIELD_OWNER, FIELD_DECIDE, FIELD_NOTE, FIELD_PROG_NOTE,
]

STATUS_TRANSITION = {"未開始": "2", "進行中": "3", "已完成": "5"}

# ── 初始化 session state ──
if "show_chat" not in st.session_state:
    st.session_state.show_chat = False
if "ad_msg" not in st.session_state:
    st.session_state.ad_msg = []
if "ad_hist" not in st.session_state:
    st.session_state.ad_hist = []
if "pending_update" not in st.session_state:
    st.session_state.pending_update = None

# ── 按鈕列 ──
btn_area, _ = st.columns([1, 4])
with btn_area:
    c1, c2 = st.columns([1.3, 1])
    with c1:
        if st.button("💬 Dashboard 小幫手", type="primary" if st.session_state.show_chat else "secondary", use_container_width=True):
            st.session_state.show_chat = not st.session_state.show_chat
            st.rerun()
    with c2:
        if st.button("🔄 更新資料", use_container_width=True):
            st.cache_data.clear()
            st.rerun()


def _doc_to_text(v):
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


def calc_overdue_days(status, end_date, actual_date=None, today=None):
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
    res = requests.get(f"{JIRA_BASE}/project/search", auth=AUTH, headers=HEADERS, params={"maxResults": 100})
    res.raise_for_status()
    return [p for p in res.json().get("values", []) if p["key"] not in EXCLUDE_PROJECT_KEYS]


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
                params = {"jql": f'project = "{proj_key}" ORDER BY created ASC', "fields": ",".join(REQUEST_FIELDS), "maxResults": 100}
                if next_page_token:
                    params["nextPageToken"] = next_page_token
                res = requests.get(f"{JIRA_BASE}/search/jql", auth=AUTH, headers=HEADERS, params=params)
                res.raise_for_status()
                data = res.json()
                for issue in data.get("issues", []):
                    f = issue["fields"]
                    status = (f.get("status") or {}).get("name") or "未開始"
                    prio = _doc_to_text(f.get(FIELD_PRIORITY)) or (f.get("priority") or {}).get("name", "")
                    end_d = f.get(FIELD_END)
                    actual_end = f.get(FIELD_ACTUAL_END)
                    progress = 100 if status == "已完成" else (0 if status == "未開始" else 50)
                    tasks.append({
                        "issue_key": issue["key"],
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
                if data.get("isLast", True) or not next_page_token or not data.get("issues"):
                    break
        except Exception as e:
            errors.append(f"{proj_name}: {e}")
    return tasks, errors


def update_jira_issue(issue_key, updates):
    """updates: dict，key 為欄位名稱，value 為新值"""
    errors = []
    fields_payload = {}

    for field, value in updates.items():
        if field == "狀態":
            tid = STATUS_TRANSITION.get(value)
            if tid:
                res = requests.post(
                    f"{JIRA_BASE}/issue/{issue_key}/transitions",
                    auth=AUTH, headers=HEADERS,
                    json={"transition": {"id": tid}}
                )
                if not res.ok:
                    errors.append(f"狀態更新失敗：{res.text}")
        elif field == "負責人":
            fields_payload[FIELD_OWNER] = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": value}]}]}
        elif field == "結束日":
            fields_payload[FIELD_END] = value
        elif field == "進度說明":
            fields_payload[FIELD_PROG_NOTE] = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": value}]}]}
        elif field == "優先":
            fields_payload[FIELD_PRIORITY] = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": value}]}]}
        elif field == "須決議":
            fields_payload[FIELD_DECIDE] = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": value}]}]}

    if fields_payload:
        res = requests.put(
            f"{JIRA_BASE}/issue/{issue_key}",
            auth=AUTH, headers=HEADERS,
            json={"fields": fields_payload}
        )
        if not res.ok:
            errors.append(f"欄位更新失敗：{res.text}")

    return errors


with st.spinner("從 Jira 載入資料中..."):
    tasks, errors = fetch_all_tasks()

for e in errors:
    st.warning(f"⚠️ {e}")

if not tasks:
    st.error("無法載入任何任務資料")
    st.stop()

today_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
tasks_json = json.dumps(tasks, ensure_ascii=False).replace("</", "<\\/")

HTML_PATH = Path(__file__).parent / "dashboard.html"
if not HTML_PATH.exists():
    st.error(f"找不到 {HTML_PATH.name}")
    st.stop()

html = HTML_PATH.read_text(encoding="utf-8")
html = html.replace("__SNAPSHOT_DATETIME__", today_str)
html = html.replace("__TASKS_JSON__", tasks_json)
html = html.replace("__GEMINI_API_KEY__", GEMINI_API_KEY)

# ── Dashboard 小幫手 ──
if st.session_state.show_chat:
    chat_col, dash_col = st.columns([1, 2])
    with dash_col:
        components.html(html, height=1200, scrolling=False)
    with chat_col:
        st.markdown("""
        <style>
        .chat-header {
            background: white; border: 1px solid #e8eaed; box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            border-radius: 12px;
            padding: 16px 20px;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .chat-header img { width: 32px; height: 32px; object-fit: contain; }
        .chat-header h3 { color: #202124; margin: 0; font-size: 16px; font-weight: 600; }
        .chat-header p { color: #80868b; margin: 0; font-size: 12px; }
        </style>
        <div class="chat-header">
            <img src="https://raw.githubusercontent.com/HarryYang-ALP/AD-chatbot/main/logo.png" alt="ALP">
            <div>
                <h3>Dashboard 小幫手</h3>
                <p>可詢問專案進度或直接更新任務資料</p>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # 確認更新的 UI
        if st.session_state.pending_update:
            pu = st.session_state.pending_update
            st.warning(f"**確認修改：**\n\n任務：**{pu['task']}**（{pu['issue_key']}）\n\n修改內容：{pu['description']}")
            col_y, col_n = st.columns(2)
            with col_y:
                if st.button("✅ 確認", use_container_width=True):
                    errs = update_jira_issue(pu["issue_key"], pu["updates"])
                    if errs:
                        st.error("\n".join(errs))
                    else:
                        st.success("✅ 更新成功！")
                        st.session_state.pending_update = None
                        st.session_state.ad_msg.append({"role": "assistant", "content": f"✅ 已成功更新 **{pu['task']}**：{pu['description']}"})
                        st.cache_data.clear()
                        st.rerun()
            with col_n:
                if st.button("❌ 取消", use_container_width=True):
                    st.session_state.pending_update = None
                    st.session_state.ad_msg.append({"role": "assistant", "content": "已取消更新。"})
                    st.rerun()

        chat_container = st.container(height=400)
        with chat_container:
            for m in st.session_state.ad_msg:
                with st.chat_message(m["role"]):
                    st.markdown(m["content"])

        if prompt := st.chat_input("問我專案進度，或說「把 XX 任務狀態改成進行中」", key="ad_chat"):
            st.session_state.ad_msg.append({"role": "user", "content": prompt})
            st.session_state.ad_hist.append({"role": "user", "parts": [{"text": prompt}]})
            with chat_container:
                with st.chat_message("user"):
                    st.markdown(prompt)

            # 任務清單摘要給 AI
            tasks_summary = json.dumps([{
                "issue_key": t.get("issue_key", ""),
                "專案": t.get("proj", ""),
                "任務": t.get("task", ""),
                "狀態": t.get("status", ""),
                "負責人": t.get("owner", ""),
                "進度": str(t.get("progress", "")) + "%",
                "結束日": t.get("end", ""),
                "逾期天數": t.get("overdue_days", 0),
                "須決議": t.get("decide", ""),
                "優先": t.get("prio", ""),
                "進度說明": t.get("prog_note", "")
            } for t in tasks], ensure_ascii=False)

            _sys = f"""你是 BPM Team 的專案進度助理，可以回答問題也可以協助更新 Jira 任務資料。
資料快照：{today_str}
任務資料：{tasks_summary}

【回答規則】
1. 若使用者在問問題，用繁體中文簡潔回答。回答時不要顯示 Jira issue key（如 BPM-8、ALPMOPT-1 等），只用專案名稱和任務名稱表示。
2. 若使用者要修改任務資料，請回傳以下 JSON 格式（只回傳 JSON，不要其他文字）：
{{
  "action": "update",
  "issue_key": "BPM-X",
  "task": "任務名稱",
  "description": "把XX改成YY",
  "updates": {{
    "狀態": "已完成"
  }}
}}
3. 嚴格規則：
   - 只修改使用者明確指定的那一個任務，絕對不能同時修改其他任務。
   "進度" 欄位由系統根據狀態自動計算（已完成=100%，未開始=0%，進行中=50%），不需要也不能單獨修改進度。
   - 可修改的欄位只有：狀態（未開始/進行中/已完成）、負責人、結束日（YYYY-MM-DD）、進度說明、優先、須決議。
   - 一次只處理一個任務的修改指令，若使用者提到多個任務請分次確認。
4. 若找不到對應任務請說明。"""

            _reply = "抱歉，發生錯誤。"
            with chat_container:
                with st.chat_message("assistant"):
                    with st.spinner("處理中..."):
                        try:
                            _r = requests.post(
                                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}",
                                json={"system_instruction": {"parts": [{"text": _sys}]}, "contents": st.session_state.ad_hist},
                                timeout=30
                            )
                            _reply = _r.json()["candidates"][0]["content"]["parts"][0]["text"]

                            # 嘗試解析是否為更新指令
                            try:
                                _clean = _reply.strip().strip("```json").strip("```").strip()
                                _cmd = json.loads(_clean)
                                if _cmd.get("action") == "update":
                                    st.session_state.pending_update = _cmd
                                    _reply = f"我準備幫你修改 **{_cmd['task']}**：{_cmd['description']}\n\n請確認是否執行？"
                            except Exception:
                                pass  # 不是 JSON，當一般回答處理

                            st.markdown(_reply)
                        except Exception as e:
                            _reply = f"錯誤：{e}"
                            st.markdown(_reply)

            st.session_state.ad_msg.append({"role": "assistant", "content": _reply})
            st.session_state.ad_hist.append({"role": "model", "parts": [{"text": _reply}]})
            st.rerun()
else:
    components.html(html, height=1200, scrolling=False)
