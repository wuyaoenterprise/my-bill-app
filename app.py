import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json

# --- 页面配置 ---
st.set_page_config(page_title="云端AA记账", page_icon="☁️")

# --- 🔐 登录保护 (密码 8888) ---
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False
    if not st.session_state.password_correct:
        pwd = st.text_input("请输入房间密码", type="password")
        if st.button("进入"):
            if pwd == "8888":
                st.session_state.password_correct = True
                st.rerun()
        return False
    return True

if not check_password():
    st.stop()

# --- ☁️ 连接 Google Sheets ---
# 使用 @st.cache_resource 保证只连接一次，不用每次刷新都连
@st.cache_resource
def get_google_sheet():
    # 从 Streamlit Secrets 里读取钥匙信息
    key_dict = json.loads(st.secrets["textkey"])
    
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(key_dict, scope)
    client = gspread.authorize(creds)
    
    # 打开你的表格 (请确保表格名字和这里一致！)
    sheet = client.open("MySplitwiseDB") 
    return sheet

try:
    sheet = get_google_sheet()
    worksheet_users = sheet.worksheet("users")
    worksheet_expenses = sheet.worksheet("expenses")
except Exception as e:
    st.error("无法连接数据库，请检查 Secrets 配置或表格分享权限。")
    st.stop()

st.title("☁️ 云端同步记账")

# --- 1. 读取数据 ---
# 每次刷新页面，都从云端拉取最新数据
users_data = worksheet_users.get_all_records()
expenses_data = worksheet_expenses.get_all_records()

user_list = [row['name'] for row in users_data]

# --- 2. 侧边栏：添加用户 ---
with st.sidebar:
    st.header("添加成员")
    new_user = st.text_input("输入名字")
    if st.button("添加"):
        if new_user and new_user not in user_list:
            st.info("正在保存到云端...")
            worksheet_users.append_row([new_user]) # 写入 Google Sheet
            st.success(f"已添加: {new_user}")
            st.rerun() # 刷新页面获取最新数据
        elif new_user in user_list:
            st.warning("该成员已存在")
    
    st.write("当前成员:", ", ".join(user_list))

# --- 3. 记录支出 ---
st.header("记录一笔支出")

if len(user_list) < 2:
    st.info("请先在侧边栏添加至少两名成员。")
else:
    col1, col2, col3 = st.columns(3)
    with col1:
        payer = st.selectbox("谁付的钱?", user_list)
    with col2:
        amount = st.number_input("金额", min_value=0.01, step=1.0)
    with col3:
        description = st.text_input("备注")

    beneficiaries = st.multiselect("谁参与了?", user_list, default=user_list)

    if st.button("添加账单"):
        if amount > 0 and beneficiaries:
            st.info("正在写入数据库...")
            # 存入 Google Sheet: 支付人, 金额, 参与人(逗号拼起来), 备注
            new_row = [payer, amount, ",".join(beneficiaries), description]
            worksheet_expenses.append_row(new_row)
            st.success("保存成功！")
            st.rerun()
        else:
            st.error("信息不完整")

# --- 4. 显示账单 ---
if expenses_data:
    st.markdown("---")
    st.subheader("📝 历史账单")
    df = pd.DataFrame(expenses_data)
    st.table(df)

# --- 5. 计算结果 ---
st.markdown("---")
st.header("💰 结算结果")

if st.button("计算分账"):
    balances = {u: 0.0 for u in user_list}
    
    for exp in expenses_data:
        p = exp['payer']
        amt = float(exp['amount']) # 确保是数字
        # 从字符串还原列表: "A,B,C" -> ['A', 'B', 'C']
        peeps = exp['for_whom'].split(",") if isinstance(exp['for_whom'], str) else []
        
        if peeps:
            split = amt / len(peeps)
            balances[p] += amt
            for person in peeps:
                if person in balances: # 防止旧数据的用户被删导致报错
                    balances[person] -= split

    # 简易贪心算法
    creditors = []
    debtors = []
    for p, amt in balances.items():
        if amt > 0.01: creditors.append([p, amt])
        elif amt < -0.01: debtors.append([p, amt])

    creditors.sort(key=lambda x: x[1], reverse=True)
    debtors.sort(key=lambda x: x[1])

    transactions = []
    i = 0
    j = 0
    while i < len(creditors) and j < len(debtors):
        c_name, c_amt = creditors[i]
        d_name, d_amt = debtors[j]
        pay = min(c_amt, -d_amt)
        transactions.append(f"**{d_name}** 给 **{c_name}**: {pay:.2f}")
        creditors[i][1] -= pay
        debtors[j][1] += pay
        if creditors[i][1] < 0.01: i += 1
        if debtors[j][1] > -0.01: j += 1
            
    if not transactions:
        st.success("账目已平！")
    else:
        for t in transactions:
            st.info(t)