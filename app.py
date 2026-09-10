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

# ⚠️ 重要：不同專案即使欄位「顯示名稱」相同（例如都叫「結束日期」），
# Jira 背後產生的 customfield ID 可能完全不同（實測發現 BPM 用 customfield_10048，
# AHP 卻是 customfield_10137）。原因是在 Team-managed 專案加欄位時，
# 就算打一樣的名字，Jira 也可能建立一個全新的自訂欄位，而不是重複使用既有的。
# 因此不能把 ID 寫死，必須在執行時依「顯示名稱」動態查詢每個專案實際對應的 ID。
FIELD_DISPLAY_NAMES = {
    "start": "開始日期",
    "end": "結束日期",
    "actual_end": "實際完成日",
    "owner": "負責人",
    "decide": "須優先決議",
    "note": "決議事項說明",
    "prog_note": "進度說明",
    "priority_custom": "優先順序",
}

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
def get_project_field_map(proj_key: str):
    """依「顯示名稱」動態解析這個專案實際使用的 customfield ID。

    做法：先抓這個專案任一筆 issue 的 key，再用
    GET /rest/api/3/issue/{key}?expand=names 取得「欄位ID -> 顯示名稱」的對照，
    反查出我們關心的幾個欄位（結束日期、負責人...）在這個專案裡對應的實際 ID。

    注意：官方文件說 /search/jql 的 expand=names 也能拿到這個對照表，
    但實測（含 Atlassian 社群回報）目前這個參數在新版搜尋 API 上是壞的、回傳空值，
    所以改用單筆 issue 的 expand=names，這個是有效的。
    """
    res = requests.get(
        f"{JIRA_BASE}/search/jql",
        auth=AUTH, headers=HEADERS,
        params={
            "jql": f'project = "{proj_key}" ORDER BY created ASC',
            "fields": "summary",
            "maxResults": 1,
        },
    )
    res.raise_for_status()
    issues = res.json().get("issues", [])
    if not issues:
        return {}

    sample_key = issues[0]["key"]
    res2 = requests.get(
        f"{JIRA_BASE}/issue/{sample_key}",
        auth=AUTH, headers=HEADERS, params={"expand": "names"},
    )
    res2.raise_for_status()
    names = res2.json().get("names", {})  # field_id -> 顯示名稱

    display_to_id = {}
    for field_id, display_name in names.items():
        if display_name in FIELD_DISPLAY_NAMES.values() and display_name not in display_to_id:
            display_to_id[display_name] = field_id

    # 轉成用我們自己好記的 key（start/end/owner...）對應到這個專案實際的 field_id
    return {key: display_to_id[name] for key, name in FIELD_DISPLAY_NAMES.items() if name in display_to_id}


@st.cache_data(ttl=300)
def fetch_all_tasks():
    tasks = []
    errors = []

    for proj in fetch_projects():
        proj_key = proj["key"]
        proj_name = proj["name"]
        try:
            field_map = get_project_field_map(proj_key)
            # 這個專案裡，上述欄位有出現的才需要跟 Jira 要，其餘固定要 summary/status/priority
            wanted_field_ids = list(set(field_map.values()))
            request_fields = ["summary", "status", "priority"] + wanted_field_ids

            next_page_token = None
            while True:
                params = {
                    "jql": f'project = "{proj_key}" ORDER BY created ASC',
                    "fields": ",".join(request_fields),
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

                    def get_field(key):
                        fid = field_map.get(key)
                        return f.get(fid) if fid else None

                    status = (f.get("status") or {}).get("name") or "未開始"

                    prio_custom = _doc_to_text(get_field("priority_custom"))
                    prio_native = (f.get("priority") or {}).get("name", "")
                    prio = prio_custom or prio_native

                    end_d = get_field("end")
                    actual_end = get_field("actual_end")

                    progress = 100 if status == "已完成" else (0 if status == "未開始" else 50)

                    tasks.append({
                        "proj": proj_name,
                        "task": f.get("summary", ""),
                        "owner": _doc_to_text(get_field("owner")),
                        "prio": prio,
                        "status": status,
                        "start": get_field("start"),
                        "end": end_d,
                        "progress": progress,
                        "decide": _doc_to_text(get_field("decide")) or "否",
                        "note": _doc_to_text(get_field("note")),
                        "prog_note": _doc_to_text(get_field("prog_note")),
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

components.html(html, height=1200, scrolling=False)
