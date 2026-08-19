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
html = html.replace("__GEMINI_API_KEY__", st.secrets.get("GEMINI_API_KEY", ""))

components.html(html, height=1200, scrolling=False)

# ── 浮動 AD 小幫手 ──
GEMINI_KEY = st.secrets.get("GEMINI_API_KEY", "")
LOGO = "https://raw.githubusercontent.com/HarryYang-ALP/AD-chatbot/main/logo.png"

_chatbot = """
<style>
#ad-fab{
  position:fixed;bottom:28px;left:28px;
  width:52px;height:52px;border-radius:50%;
  background:white;border:1.5px solid #e0e0e0;
  box-shadow:0 4px 16px rgba(0,0,0,0.15);
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  z-index:9999;
}
#ad-fab img{width:30px;height:30px;object-fit:contain;}
#ad-chat-window{
  position:fixed;bottom:92px;left:28px;
  width:360px;height:500px;
  background:white;border-radius:16px;
  box-shadow:0 8px 40px rgba(0,0,0,0.18);
  border:1px solid #e8eaed;
  display:none;flex-direction:column;
  z-index:9998;overflow:hidden;
}
#ad-chat-header{
  padding:13px 16px;background:#1a73e8;
  display:flex;align-items:center;gap:10px;flex-shrink:0;
}
#ad-chat-header img{width:24px;height:24px;object-fit:contain;background:white;border-radius:50%;padding:2px;}
#ad-chat-header span{color:white;font-size:14px;font-weight:600;flex:1;}
#ad-chat-close{color:white;cursor:pointer;font-size:18px;background:none;border:none;padding:0;}
#ad-chat-messages{
  flex:1;overflow-y:auto;padding:14px;
  display:flex;flex-direction:column;gap:10px;background:#f7f8fa;
}
.ad-msg-row{display:flex;gap:8px;align-items:flex-end;}
.ad-msg-row.user{justify-content:flex-end;}
.ad-msg-bubble{
  max-width:80%;padding:9px 13px;font-size:13px;line-height:1.6;
  border-radius:0 12px 12px 12px;
  background:white;border:1px solid #e8eaed;color:#202124;
}
.ad-msg-row.user .ad-msg-bubble{
  background:#e8f0fe;border:none;border-radius:12px 0 12px 12px;
}
.ad-typing{font-size:12px;color:#80868b;padding:2px 4px;}
.ad-bubble-area{padding:8px 12px;background:white;border-top:1px solid #f0f0f0;flex-shrink:0;}
.ad-bubble-label{font-size:11px;color:#80868b;margin-bottom:6px;}
.ad-bubble-wrap{display:flex;flex-wrap:wrap;gap:5px;}
.ad-bubble-btn{padding:4px 10px;font-size:12px;background:white;border:1px solid #dadce0;border-radius:14px;cursor:pointer;color:#1a73e8;}
.ad-bubble-btn:hover{background:#e8f0fe;border-color:#1a73e8;}
#ad-chat-input-area{
  padding:10px 12px;border-top:1px solid #e8eaed;
  background:white;flex-shrink:0;display:flex;gap:8px;align-items:center;
}
#ad-chat-input{
  flex:1;padding:8px 14px;border:1px solid #dadce0;border-radius:20px;
  font-size:13px;outline:none;background:#fafafa;font-family:inherit;
}
#ad-chat-input:focus{border-color:#1a73e8;background:white;}
#ad-send-btn{
  width:32px;height:32px;border-radius:50%;background:#1a73e8;
  border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;
}
#ad-send-btn svg{width:14px;height:14px;fill:white;}
</style>

<div id="ad-fab"><img src="__LOGO__" alt="AD"></div>

<div id="ad-chat-window">
  <div id="ad-chat-header">
    <img src="__LOGO__" alt="ALP">
    <span>AD 小幫手</span>
    <button id="ad-chat-close">✕</button>
  </div>
  <div id="ad-chat-messages">
    <div class="ad-msg-row">
      <div class="ad-msg-bubble">你好！我是 AD 小幫手，有任何 BPM 或行政流程問題都可以問我 😊</div>
    </div>
  </div>
  <div class="ad-bubble-area" id="ad-bubble-area">
    <div class="ad-bubble-label">💡 你可以這樣問：</div>
    <div class="ad-bubble-wrap" id="ad-bubble-wrap"></div>
  </div>
  <div id="ad-chat-input-area">
    <input id="ad-chat-input" type="text" placeholder="請輸入你的問題...">
    <button id="ad-send-btn"><svg viewBox="0 0 24 24"><path d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg></button>
  </div>
</div>

<script>
(function(){
  var API_KEY = "__GEMINI_KEY__";
  var API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key=" + API_KEY;
  var SYSTEM = "你是 ALP 公司的 AD 小幫手，專門回答 BPM 系統操作與行政流程相關問題。請用繁體中文回答，清楚簡潔。若超出知識範圍請告知洽 AD 團隊。知識庫：BPM網址 https://bpm.alp.global / 登入用 Azure AD Login+M365 Email / 代理人設定：Personal>Account>Leaving設Out of Office，Task Rules加Delegation規則 / Claim Task取得Share Task處理權 / 採購單SAP-MM判定：主營業務、資產、預付訂閱 / 一般採購LOA：3萬以下主管,3萬-30萬採購成控+主管,30萬-500萬+營運長,500萬-3000萬+執行長,3000萬以上+董事長 / 出差3工作天前申請，含住宿才用BPM";

  var BUBBLES = {
    "default":["如何登入 BPM？","採購單怎麼填？","如何設定代理人？","出差申請流程？","核決權限？","驗收單怎麼開？"],
    "採購":["核決權限是多少？","WBS Code 怎麼選？","如何撤回？"],
    "核決":["一般採購核決？","物管採購核決？","請款核決？"],
    "出差":["出差簽核流程？","當日來回怎麼申請？"],
    "代理":["代理人設定步驟？","如何取消代理？"]
  };

  var adHistory = [], adOpen = false;

  function toggleAdChat(){
    adOpen = !adOpen;
    var win = document.getElementById("ad-chat-window");
    win.style.display = adOpen ? "flex" : "none";
    if(adOpen){ adRenderBubbles("default"); document.getElementById("ad-chat-input").focus(); }
  }

  function adRenderBubbles(hint){
    var set = BUBBLES["default"];
    for(var k in BUBBLES){
      if(k !== "default" && hint.indexOf(k) >= 0){ set = BUBBLES[k]; break; }
    }
    document.getElementById("ad-bubble-wrap").innerHTML = set.map(function(q){
      return '<button class="ad-bubble-btn" onclick="window._adSendQ(this.textContent)">' + q + "</button>";
    }).join("");
  }

  window._adSendQ = function(q){
    document.getElementById("ad-chat-input").value = q;
    adSendMsg();
  };

  function adAddMsg(text, isUser){
    var msgs = document.getElementById("ad-chat-messages");
    var row = document.createElement("div");
    row.className = "ad-msg-row" + (isUser ? " user" : "");
    var b = document.createElement("div");
    b.className = "ad-msg-bubble";
    b.innerHTML = text.replace(/\n/g,"<br>");
    row.appendChild(b);
    msgs.appendChild(row);
    msgs.scrollTop = msgs.scrollHeight;
  }

  function adSendMsg(){
    var inp = document.getElementById("ad-chat-input");
    var text = inp.value.trim();
    if(!text) return;
    inp.value = "";
    document.getElementById("ad-bubble-area").style.display = "none";
    adAddMsg(text, true);
    adHistory.push({role:"user", parts:[{text:text}]});
    var msgs = document.getElementById("ad-chat-messages");
    var tr = document.createElement("div");
    tr.className = "ad-msg-row";
    var td = document.createElement("div");
    td.className = "ad-typing";
    td.textContent = "思考中...";
    tr.appendChild(td);
    msgs.appendChild(tr);
    msgs.scrollTop = 99999;
    fetch(API_URL,{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({system_instruction:{parts:[{text:SYSTEM}]},contents:adHistory})
    }).then(function(r){return r.json();}).then(function(data){
      var reply = data.candidates&&data.candidates[0]&&data.candidates[0].content&&data.candidates[0].content.parts&&data.candidates[0].content.parts[0].text||"抱歉，無法取得回應。";
      tr.remove();
      adAddMsg(reply, false);
      adHistory.push({role:"model",parts:[{text:reply}]});
      document.getElementById("ad-bubble-area").style.display = "block";
      adRenderBubbles(text);
    }).catch(function(){
      tr.remove();
      adAddMsg("發生錯誤，請稍後再試。", false);
    });
  }

  document.getElementById("ad-fab").addEventListener("click", toggleAdChat);
  document.getElementById("ad-chat-close").addEventListener("click", toggleAdChat);
  document.getElementById("ad-send-btn").addEventListener("click", adSendMsg);
  document.getElementById("ad-chat-input").addEventListener("keydown", function(e){
    if(e.key === "Enter") adSendMsg();
  });
})();
</script>
"""

_chatbot = _chatbot.replace("__LOGO__", LOGO).replace("__GEMINI_KEY__", GEMINI_KEY)
components.html(_chatbot, height=0)
