import streamlit as st
import pandas as pd

# --- 页面设置 ---
st.set_page_config(page_title="简易AA记账", page_icon="💰")

# --- 🔐 简单的登录保护 ---
def check_password():
    """如果不输入正确密码，就不能看账本"""
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False

    if not st.session_state.password_correct:
        st.title("🔒 请登录")
        password = st.text_input("请输入房间密码", type="password")
        if st.button("进入"):
            # 设定密码为 8888 (你可以自己改)
            if password == "8888":
                st.session_state.password_correct = True
                st.rerun()
            else:
                st.error("密码错误")
        return False
    return True

if not check_password():
    st.stop()  # 如果没登录，下面的代码都不运行

# ==========================================
# 下面是之前的记账逻辑，登录后才会显示
# ==========================================

st.title("💰 简易AA记账神器")

# --- 1. 初始化数据 ---
if 'users' not in st.session_state:
    st.session_state.users = []
if 'expenses' not in st.session_state:
    st.session_state.expenses = []

# --- 2. 侧边栏：添加用户 ---
with st.sidebar:
    st.header("1. 添加成员")
    new_user = st.text_input("输入名字")
    if st.button("添加成员"):
        if new_user and new_user not in st.session_state.users:
            st.session_state.users.append(new_user)
            st.success(f"已添加: {new_user}")
        elif new_user in st.session_state.users:
            st.warning("该成员已存在")
    
    st.write("当前成员:", ", ".join(st.session_state.users))
    
    if st.button("重置所有数据"):
        st.session_state.users = []
        st.session_state.expenses = []
        st.rerun()

# --- 3. 主界面：记录支出 ---
st.header("2. 记录一笔支出")

if len(st.session_state.users) < 2:
    st.info("👈 请先在左侧侧边栏添加至少两名成员。")
else:
    col1, col2, col3 = st.columns(3)
    with col1:
        payer = st.selectbox("谁付的钱?", st.session_state.users)
    with col2:
        amount = st.number_input("金额 (元)", min_value=0.01, step=1.0)
    with col3:
        description = st.text_input("备注 (例如: 晚餐)")

    beneficiaries = st.multiselect("谁参与了消费? (默认全员)", st.session_state.users, default=st.session_state.users)

    if st.button("添加账单"):
        if amount > 0 and beneficiaries:
            expense = {
                "payer": payer,
                "amount": amount,
                "for_whom": beneficiaries,
                "desc": description
            }
            st.session_state.expenses.append(expense)
            st.success("账单已记录！")
        else:
            st.error("请输入金额并选择参与人。")

# --- 4. 显示账单列表 ---
if st.session_state.expenses:
    st.markdown("---")
    st.subheader("📝 账单明细")
    df = pd.DataFrame(st.session_state.expenses)
    st.table(df)

# --- 5. 核心算法：计算结果 ---
st.markdown("---")
st.header("3. 结算结果 (谁给谁钱)")

if st.button("计算分账"):
    balances = {u: 0.0 for u in st.session_state.users}
    for exp in st.session_state.expenses:
        paid_by = exp['payer']
        total = exp['amount']
        people = exp['for_whom']
        if len(people) > 0:
            split_amount = total / len(people)
            balances[paid_by] += total
            for person in people:
                balances[person] -= split_amount

    creditors = []
    debtors = []
    for person, amount in balances.items():
        if amount > 0.01: creditors.append([person, amount])
        elif amount < -0.01: debtors.append([person, amount])

    creditors.sort(key=lambda x: x[1], reverse=True)
    debtors.sort(key=lambda x: x[1])

    transactions = []
    i = 0
    j = 0
    while i < len(creditors) and j < len(debtors):
        creditor_name, credit_amount = creditors[i]
        debtor_name, debt_amount = debtors[j]
        amount_to_pay = min(credit_amount, -debt_amount)
        transactions.append(f"**{debtor_name}** 应支付给 **{creditor_name}**: {amount_to_pay:.2f} 元")
        creditors[i][1] -= amount_to_pay
        debtors[j][1] += amount_to_pay
        if creditors[i][1] < 0.01: i += 1
        if debtors[j][1] > -0.01: j += 1
            
    if not transactions:
        st.success("账目已平，不需要转账！")
    else:
        for trans in transactions:
            st.info(trans)