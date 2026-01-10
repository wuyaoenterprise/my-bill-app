import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Boolean, DateTime, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, joinedload, scoped_session
from datetime import datetime, date, time as dt_time
import uuid
import collections
import heapq
import time
from streamlit_oauth import OAuth2Component # 新增：用于登录

# ==========================================
# 🏗️ 1. 底层架构 (Database Models) - 保持不变
# ==========================================
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(String, primary_key=True) # 这里现在存储 Google Email
    username = Column(String, nullable=False) # 存储显示名
    created_at = Column(DateTime, default=datetime.now)

class Group(Base):
    __tablename__ = 'groups'
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    is_deleted = Column(Boolean, default=False)
    members = relationship("GroupMember", back_populates="group", cascade="all, delete")

class GroupMember(Base):
    __tablename__ = 'group_members'
    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(String, ForeignKey('groups.id'))
    user_id = Column(String, ForeignKey('users.id'))
    group = relationship("Group", back_populates="members")
    user = relationship("User")

class Expense(Base):
    __tablename__ = 'expenses'
    id = Column(String, primary_key=True)
    group_id = Column(String, ForeignKey('groups.id'))
    created_by = Column(String, ForeignKey('users.id'))
    description = Column(String, nullable=False)
    amount = Column(BigInteger, nullable=False) # 存储为分
    category = Column(String) 
    date = Column(DateTime, default=datetime.now)
    is_deleted = Column(Boolean, default=False)
    splits = relationship("Split", back_populates="expense", cascade="all, delete")
    creator = relationship("User")

class Split(Base):
    __tablename__ = 'splits'
    id = Column(Integer, primary_key=True, autoincrement=True)
    expense_id = Column(String, ForeignKey('expenses.id'))
    user_id = Column(String, ForeignKey('users.id'))
    paid_amount = Column(BigInteger, default=0)
    owed_amount = Column(BigInteger, default=0)
    expense = relationship("Expense", back_populates="splits")
    user = relationship("User")

# ==========================================
# 🚀 数据库连接优化版 (核心升级：解决延迟)
# ==========================================
@st.cache_resource
def get_db_engine():
    db_url = st.secrets.get("DATABASE_URL", 'sqlite:///splitwise_pro.db')
    
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    
    # 增加 pool_recycle 防止连接过期导致的卡顿
    return create_engine(
        db_url, 
        pool_pre_ping=True, 
        pool_size=10, 
        max_overflow=20, 
        pool_recycle=3600
    )

engine = get_db_engine()
Base.metadata.create_all(engine)

# ⚠️ 关键修改：使用 scoped_session 实现线程隔离
# 这能确保每个用户的操作都在独立的 DB 会话中，互不干扰，彻底解决延迟
session_factory = sessionmaker(bind=engine)
Session = scoped_session(session_factory)

# ==========================================
# 🧠 2. 核心财务引擎 - 保持完全不变
# ==========================================
class FinanceEngine:
    @staticmethod
    def to_cents(amount_float): return int(round(amount_float * 100))
    @staticmethod
    def to_dollars(amount_int): return amount_int / 100.0

    @staticmethod
    def distribute_amount(total_cents, weights):
        total_weight = sum(weights)
        if total_weight == 0: return [0] * len(weights)
        
        amounts = []
        current_sum = 0
        for w in weights:
            share = int((total_cents * w) / total_weight)
            amounts.append(share)
            current_sum += share
            
        remainder = total_cents - current_sum
        for i in range(remainder):
            amounts[i] += 1
            
        return amounts

    @staticmethod
    def simplify_debts(net_balances):
        debtors = []
        creditors = []
        for person, amount in net_balances.items():
            if amount < -1: heapq.heappush(debtors, (amount, person))
            elif amount > 1: heapq.heappush(creditors, (-amount, person))
        
        transactions = []
        while debtors and creditors:
            debt_amt, debtor = heapq.heappop(debtors)
            credit_amt, creditor = heapq.heappop(creditors)
            amount = min(-debt_amt, -credit_amt)
            transactions.append({"from": debtor, "to": creditor, "amount": amount})
            
            remain_debt = debt_amt + amount
            remain_credit = credit_amt + amount
            
            if remain_debt < -1: heapq.heappush(debtors, (remain_debt, debtor))
            if remain_credit < -1: heapq.heappush(creditors, (remain_credit, creditor))
        return transactions

# ==========================================
# 🛠️ 3. 业务服务层 (升级：隐私过滤 & Session管理)
# ==========================================
# 辅助装饰器：自动管理 Session 生命周期
def with_session(func):
    def wrapper(*args, **kwargs):
        session = Session() # 获取当前线程的 session
        try:
            return func(session, *args, **kwargs)
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close() # 必须关闭以释放连接池
    return wrapper

class GroupService:
    @staticmethod
    @with_session
    def create_group(session, name, user_emails):
        try:
            grp = Group(id=str(uuid.uuid4()), name=name)
            session.add(grp)
            
            # 确保当前用户也在列表里
            current_email = st.session_state.user_email
            if current_email not in user_emails:
                user_emails.append(current_email)
            
            # 核心逻辑：遍历邮箱，如果用户不存在，自动创建
            for email in set(user_emails):
                email = email.strip() # 去除空格
                if not email: continue
                
                # 检查用户是否存在，不存在则创建（利用 UserService 的逻辑）
                # 注意：这里我们手动实现一下 ensure，因为在 session 内部调用 service 可能会有嵌套 session 问题
                u = session.query(User).filter_by(id=email).first()
                if not u:
                    u = User(id=email, username=email.split('@')[0]) # 默认用邮箱前缀做用户名
                    session.add(u)
                
                # 添加到群组
                session.add(GroupMember(group_id=grp.id, user_id=email))
            
            session.commit()
            return True, "创建成功"
        except Exception as e:
            return False, str(e)

    @staticmethod
    @with_session
    def delete_group(session, group_id):
        # 增加权限校验：只有群成员能删吗？暂时保持原逻辑
        grp = session.query(Group).filter_by(id=group_id).first()
        if grp:
            grp.is_deleted = True
            session.commit()
            return True
        return False

    @staticmethod
    @with_session
    def get_my_groups(session, user_email):
        """核心隔离：只查询我所在的群组"""
        return session.query(Group).join(GroupMember).filter(
            GroupMember.user_id == user_email,
            Group.is_deleted == False
        ).options(joinedload(Group.members).joinedload(GroupMember.user)).all()

class ExpenseService:
    @staticmethod
    @with_session
    def create_expense(session, desc, total_cents, group_id, created_by, category, payer_splits, ower_splits, custom_time=None):
        if abs(sum(payer_splits.values()) - total_cents) > 1 or abs(sum(ower_splits.values()) - total_cents) > 1:
            return False, "账目不平"

        try:
            exp_id = str(uuid.uuid4())
            final_time = custom_time if custom_time else datetime.now()
            
            expense = Expense(id=exp_id, description=desc, amount=total_cents, group_id=group_id, 
                              created_by=created_by, category=category, date=final_time)
            session.add(expense)

            all_users = set(payer_splits.keys()) | set(ower_splits.keys())
            for uid in all_users:
                p = payer_splits.get(uid, 0)
                o = ower_splits.get(uid, 0)
                if p > 0 or o > 0:
                    session.add(Split(expense_id=exp_id, user_id=uid, paid_amount=p, owed_amount=o))
            
            session.commit()
            return True, "成功"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def create_repayment(payer_id, receiver_id, amount_cents, group_id, custom_time=None):
        payer_splits = {payer_id: amount_cents}
        ower_splits = {receiver_id: amount_cents}
        # 复用上面的 create_expense，不需要 @with_session 因为它会调用带装饰器的函数
        return ExpenseService.create_expense(
            "还款", amount_cents, group_id, payer_id, "Repayment", payer_splits, ower_splits, custom_time
        )

    @staticmethod
    @with_session
    def delete_expense(session, exp_id):
        exp = session.query(Expense).filter_by(id=exp_id).first()
        if exp:
            exp.is_deleted = True
            session.commit()
            return True
        return False

    @staticmethod
    @with_session
    def get_balances(session, group_id):
        expenses = session.query(Expense).filter_by(group_id=group_id, is_deleted=False).all()
        balances = collections.defaultdict(int)
        for exp in expenses:
            for s in exp.splits:
                balances[s.user.username] += (s.paid_amount - s.owed_amount)
        return balances

    @staticmethod
    @with_session
    def get_activity(session, group_id):
        return session.query(Expense).filter_by(group_id=group_id, is_deleted=False).order_by(Expense.date.desc()).options(joinedload(Expense.creator)).all()

class UserService:
    @staticmethod
    @with_session
    def get_all(session): 
        # 获取所有用户用于拉人进群
        return session.query(User).all()
        
    @staticmethod
    @with_session
    def ensure_user_exists(session, email, name=None):
        """登录时确保用户在数据库中"""
        u = session.query(User).filter_by(id=email).first()
        if not u:
            username = name if name else email.split('@')[0]
            u = User(id=email, username=username)
            session.add(u)
            session.commit()
        return u

# ==========================================
# 🎨 4. 前端 UI (Streamlit)
# ==========================================
st.set_page_config(page_title="Splitwise Ultimate Pro", page_icon="💸", layout="wide")
st.markdown("<style>.big-font {font-size:18px !important;}</style>", unsafe_allow_html=True)

# --- 🔐 身份验证模块 (新增) ---
CLIENT_ID = st.secrets.get("GOOGLE_CLIENT_ID")
CLIENT_SECRET = st.secrets.get("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = st.secrets.get("GOOGLE_REDIRECT_URI")

if not CLIENT_ID or not CLIENT_SECRET:
    st.error("请先在 secrets.toml 配置 Google OAuth 信息")
    st.stop()

if 'user_email' not in st.session_state:
    st.title("💸 聚会分账系统 - 登录")
    st.caption("请登录以查看属于您的私有数据")
    
    oauth2 = OAuth2Component(CLIENT_ID, CLIENT_SECRET, "https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token", "https://oauth2.googleapis.com/token", REDIRECT_URI)
    result = oauth2.authorize_button(name="使用 Google 登录", scope="openid email profile", redirect_uri=REDIRECT_URI)
    
    if result and result.get("token"):
        # 解码 token 获取邮箱 (简单起见，这里假设 token 包含 id_token)
        # 实际生产中建议使用 jwt 库解码 id_token
        import base64, json
        id_token = result["token"]["id_token"]
        payload = id_token.split('.')[1]
        padded = payload + '=' * (4 - len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        
        email = decoded.get("email")
        name = decoded.get("name")
        
        UserService.ensure_user_exists(email, name)
        st.session_state.user_email = email
        st.session_state.user_name = name
        st.rerun()
    st.stop() # 未登录停止向下执行

# --- 登录成功后的主逻辑 ---
current_u_email = st.session_state.user_email
# 获取当前用户对象用于显示
current_u_obj = UserService.ensure_user_exists(current_u_email) 
current_u_name = current_u_obj.username

if 'page' not in st.session_state: st.session_state.page = "dashboard"

# --- 侧边栏 ---
with st.sidebar:
    st.title("💸 聚会分账系统")
    st.success(f"已登录: {current_u_name}")
    if st.button("退出登录"):
        del st.session_state.user_email
        st.rerun()

    st.divider()
    nav = st.radio("功能导航", ["📊 仪表盘 & 动态", "📝 记一笔 (支出)", "💸 还款 (结算)", "⚙️ 设置"])
    
    st.divider()
    # 隐私隔离：这里的全员列表仅用于展示，实际操作由 current_u_email 决定
    all_users = UserService.get_all() 

# 核心修改：只获取我所在的群组
my_groups = GroupService.get_my_groups(current_u_email)

# --- 1. 仪表盘 & 动态 ---
if nav == "📊 仪表盘 & 动态":
    st.header(f"👋 你好, {current_u_name}")
    
    if not my_groups: st.info("暂无群组，请去设置创建")
    
    for grp in my_groups:
        with st.container(border=True):
            st.subheader(f"📂 {grp.name}")
            
            # A. 余额卡片
            balances = ExpenseService.get_balances(grp.id)
            txs = FinanceEngine.simplify_debts(balances)
            
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**💰 应付账款**")
                if not txs: st.caption("目前账目已平")
                for t in txs:
                    st.info(f"👉 **{t['from']}** 需还给 **{t['to']}**: {FinanceEngine.to_dollars(t['amount'])}")
            with c2:
                st.markdown("**📊 你的状况**")
                bal = balances.get(current_u_name, 0)
                color = "green" if bal >= 0 else "red"
                txt = f"收回 {FinanceEngine.to_dollars(bal)}" if bal >= 0 else f"支付 {FinanceEngine.to_dollars(abs(bal))}"
                st.markdown(f":{color}[**需{txt}**]")

            st.divider()
            
            # B. 最近动态 (逻辑保持不变)
            st.markdown("**🕒 最近动态 (按时间倒序)**")
            activities = ExpenseService.get_activity(grp.id)
            if not activities:
                st.caption("暂无记录")
            else:
                for exp in activities:
                    time_str = exp.date.strftime('%Y-%m-%d %H:%M')
                    amt_str = f"{FinanceEngine.to_dollars(exp.amount)}"
                    
                    with st.expander(f"{time_str} | {exp.description} - {amt_str}元"):
                        col_a, col_b = st.columns([4, 1])
                        with col_a:
                            st.write(f"创建人: {exp.creator.username}")
                            st.write(f"分类: {exp.category}")
                            details = []
                            for s in exp.splits:
                                if s.paid_amount > 0: details.append(f"{s.user.username}付{FinanceEngine.to_dollars(s.paid_amount)}")
                                if s.owed_amount > 0: details.append(f"{s.user.username}耗{FinanceEngine.to_dollars(s.owed_amount)}")
                            st.caption(", ".join(details))
                        with col_b:
                            if st.button("🗑️ 删除", key=f"del_{exp.id}"):
                                ExpenseService.delete_expense(exp.id)
                                st.rerun()

# --- 2. 记一笔 (支出) ---
elif nav == "📝 记一笔 (支出)":
    st.header("📝 记录支出")
    if not my_groups: st.stop()
    
    sel_grp = st.selectbox("选择群组", [g.name for g in my_groups])
    grp = next(g for g in my_groups if g.name == sel_grp)
    members = [m.user.username for m in grp.members]
    m_ids = {m.user.username: m.user.id for m in grp.members}
    
    # 🟢 第一部分：基础信息（放在外面，方便实时更新）
    with st.container(border=True):
        c1, c2, c3 = st.columns(3)
        desc = c1.text_input("消费内容", "聚餐")
        # min_value=0.0 修复报错
        amt = c2.number_input("总金额", min_value=0.0, step=1.0, value=0.0, key='total_amt') 
        cat = c3.selectbox("分类", ["餐饮", "交通", "房租", "购物", "娱乐", "其他"])
        
        c4, c5 = st.columns(2)
        d_date = c4.date_input("日期", date.today())
        d_time = c5.time_input("时间", datetime.now().time())

    st.divider()

    # 🟢 第二部分：模式选择（必须放在 Form 外面，否则点不动！）
    st.subheader("1. 谁先垫付的?")
    pay_mode = st.radio("付款方式", ["单人垫付", "多人付款"], horizontal=True)
    
    # 这里的逻辑需要放在 form 外面，以便界面响应
    payer_splits = {}
    payer = None
    if pay_mode == "单人垫付":
        default_idx = members.index(current_u_name) if current_u_name in members else 0
        payer = st.selectbox("付款人", members, index=default_idx)
    
    st.divider()

    st.subheader("2. 怎么分 (谁该给钱)?")
    # 关键：这个 Radio 放在外面，一点就会刷新界面！
    split_method = st.radio("分账模式", ["🏁 均分 (Equal)", "🔢 按份数 (Shares)", "💯 按百分比 (%)", "💵 具体金额"], horizontal=True)

    # 🟢 第三部分：具体数据的填写（放入 Form 统一提交）
    with st.form("expense_form"):
        # A. 处理多人付款的输入框 (如果是单人，这里不显示)
        if pay_mode == "多人付款":
            st.caption("输入垫付金额：")
            pay_cols = st.columns(len(members))
            for i, m in enumerate(members):
                val = pay_cols[i].number_input(f"{m} 付了", min_value=0.0, step=1.0, key=f"pay_{m}")
                if val > 0: payer_splits[m_ids[m]] = FinanceEngine.to_cents(val)
        elif pay_mode == "单人垫付" and payer:
            # 单人模式在提交时自动计算，这里不需要输入框
            pass

        # B. 处理分账模式的输入框
        ower_splits = {}
        total_cents = FinanceEngine.to_cents(amt)
        
        # 变量初始化，防止报错
        weights = []
        active_members = []
        pcts = []
        
        # --- 逻辑 A: 均分 (直接显示结果) ---
        if split_method == "🏁 均分 (Equal)":
            involved = st.multiselect("选择参与人", members, default=members)
            if involved and total_cents > 0:
                weights_equal = [1] * len(involved)
                amounts = FinanceEngine.distribute_amount(total_cents, weights_equal)
                
                st.info("👇 自动计算结果 (无需手动填写):")
                preview_cols = st.columns(len(involved))
                for i, m in enumerate(involved):
                    ower_splits[m_ids[m]] = amounts[i]
                    preview_cols[i].metric(label=m, value=f"{FinanceEngine.to_dollars(amounts[i])}元")
            elif total_cents == 0:
                st.warning("⚠️ 请先在最上面输入【总金额】")

        # --- 逻辑 B: 按份数 (显示输入框) ---     
        elif split_method == "🔢 按份数 (Shares)":
            st.info("例如：A 吃了 2 份，B 吃了 1 份")
            cols = st.columns(len(members))
            for i, m in enumerate(members):
                w = cols[i].number_input(f"{m} 的份数", min_value=0, step=1, value=1, key=f"share_{m}")
                weights.append(w)
                active_members.append(m)

        # --- 逻辑 C: 百分比 (显示输入框) ---
        elif split_method == "💯 按百分比 (%)":
            st.info("请输入百分比 (总和需为 100%)")
            cols = st.columns(len(members))
            for i, m in enumerate(members):
                p = cols[i].number_input(f"{m} (%)", min_value=0.0, max_value=100.0, step=5.0, key=f"pct_{m}")
                pcts.append(p)

        # --- 逻辑 D: 具体金额 (显示输入框) ---
        elif split_method == "💵 具体金额":
            st.caption("手动输入每个人应付的金额")
            cols = st.columns(len(members))
            for i, m in enumerate(members):
                val = cols[i].number_input(f"{m} 应付", min_value=0.0, step=1.0, key=f"exact_{m}")
                c = FinanceEngine.to_cents(val)
                if c > 0: ower_splits[m_ids[m]] = c
        
        # --- 提交按钮 ---
        st.write("") # 留点空隙
        submitted = st.form_submit_button("✅ 确认记账", type="primary")
        
        if submitted:
            # 1. 补充付款人数据 (如果是单人垫付)
            if pay_mode == "单人垫付":
                payer_splits = {m_ids[payer]: total_cents}
            
            # 2. 补充计算逻辑 (针对 Form 内部填写的数据)
            if split_method == "🔢 按份数 (Shares)":
                if sum(weights) > 0:
                    amounts = FinanceEngine.distribute_amount(total_cents, weights)
                    for i, m in enumerate(active_members):
                        if amounts[i] > 0: ower_splits[m_ids[m]] = amounts[i]
            
            elif split_method == "💯 按百分比 (%)":
                if abs(sum(pcts) - 100.0) < 0.01:
                    weights_pct = [int(p*100) for p in pcts]
                    amounts = FinanceEngine.distribute_amount(total_cents, weights_pct)
                    for i, m in enumerate(members):
                        if amounts[i] > 0: ower_splits[m_ids[m]] = amounts[i]
                else:
                    st.error(f"百分比总和必须是 100%，当前是 {sum(pcts)}%")
                    st.stop()

            # 3. 最终校验并提交
            if not payer_splits:
                st.error("必须有付款人")
            elif not ower_splits:
                st.error("分摊金额为 0，请检查输入")
            else:
                final_dt = datetime.combine(d_date, d_time)
                success, msg = ExpenseService.create_expense(desc, total_cents, grp.id, current_u_email, cat, payer_splits, ower_splits, final_dt)
                if success:
                    st.balloons()
                    st.success("账单已保存！")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error(msg)

# --- 3. 还款 (结算) ---
elif nav == "💸 还款 (结算)":
    st.header("💸 记录还款")
    if not my_groups: st.stop()
    
    sel_grp_s = st.selectbox("选择群组", [g.name for g in my_groups], key="settle_grp")
    grp_s = next(g for g in my_groups if g.name == sel_grp_s)
    members_s = [m.user.username for m in grp_s.members]
    m_ids_s = {m.user.username: m.user.id for m in grp_s.members}
    
    c1, c2, c3 = st.columns(3)
    # 智能默认值：如果我是成员，付款人默认是我
    payer_s = c1.selectbox("付款人", members_s, index=members_s.index(current_u_name) if current_u_name in members_s else 0)
    receiver_s = c2.selectbox("收款人", members_s, index=1 if len(members_s)>1 else 0)
    amt_s = c3.number_input("还款金额", min_value=0.01, step=1.0)
    
    c4, c5 = st.columns(2)
    s_date = c4.date_input("还款日期", date.today())
    s_time = c5.time_input("还款时间", datetime.now().time())

    if st.button("✅ 确认还款", type="primary"):
        if payer_s == receiver_s:
            st.error("自己不能还给自己")
        else:
            final_dt_s = datetime.combine(s_date, s_time)
            ExpenseService.create_repayment(m_ids_s[payer_s], m_ids_s[receiver_s], 
                                          FinanceEngine.to_cents(amt_s), grp_s.id, final_dt_s)
            st.balloons()
            st.success(f"已记录：{payer_s} 还给 {receiver_s} {amt_s}元")
            time.sleep(1)
            st.rerun()

# --- 4. 设置 ---
# ... 之前的代码 ...

elif nav == "⚙️ 设置":
    st.subheader("创建新群组")
    n_grp = st.text_input("群名")
    
    # 方式 A: 从已存在的用户里选
    others = [u.username for u in all_users if u.id != current_u_email]
    invites_existing = st.multiselect("选择已注册成员", others)
    
    # 方式 B: 直接输入邮箱 (新增功能)
    st.markdown("**或者直接输入朋友的 Google 邮箱 (一行一个):**")
    invite_emails_raw = st.text_area("邮箱列表", placeholder="friend1@gmail.com\nfriend2@gmail.com")
    
    if st.button("建群"):
        if n_grp:
            # 1. 获取已选用户的邮箱 (ID)
            selected_emails = [u.id for u in all_users if u.username in invites_existing]
            
            # 2. 获取手填的邮箱
            manual_emails = [e.strip() for e in invite_emails_raw.split('\n') if '@' in e]
            
            # 3. 合并所有邀请名单
            final_invite_list = selected_emails + manual_emails
            
            # 调用升级版的 create_group
            success, msg = GroupService.create_group(n_grp, final_invite_list)
            
            if success:
                st.success(f"成功创建群组: {n_grp}")
                time.sleep(1) # 给一点时间让数据写入
                st.rerun()
            else:
                st.error(f"创建失败: {msg}")
        else:
            st.warning("请输入群名")

    st.divider()

    st.subheader("删除群组")
    if my_groups:
        del_g = st.selectbox("选择删除", [g.name for g in my_groups])
        if st.button("删除该群"):
            t_g = next(g for g in my_groups if g.name == del_g)
            GroupService.delete_group(t_g.id)
            st.rerun()
    else:
        st.info("没有可删除的群组")
        
# 扫尾工作：移除当前线程的 session，防止内存泄漏
Session.remove()



