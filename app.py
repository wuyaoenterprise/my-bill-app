import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Boolean, DateTime, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, joinedload
from datetime import datetime, date, time as dt_time
import uuid
import collections
import heapq
import time

# ==========================================
# 🏗️ 1. 底层架构 (Database Models)
# ==========================================
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(String, primary_key=True)
    username = Column(String, unique=True, nullable=False)
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
# 🚀 数据库连接优化版 (带缓存)
# ==========================================
@st.cache_resource(ttl="2h")
def get_db_engine():
    # 1. 优先尝试从云端 Secrets 获取
    db_url = st.secrets.get("DATABASE_URL")
    
    # 2. 如果没有云端配置，回退到本地 SQLite (方便你在自己电脑调试)
    if not db_url:
        return create_engine('sqlite:///splitwise_pro.db', connect_args={'check_same_thread': False})

    # 3. 修正 Supabase 链接格式
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    
    # 4. 创建连接池 (优化并发)
    return create_engine(db_url, pool_pre_ping=True, pool_size=5, max_overflow=10)

# 获取带缓存的 engine
engine = get_db_engine()

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
session = Session()

# ==========================================
# 🧠 2. 核心财务引擎
# ==========================================
class FinanceEngine:
    @staticmethod
    def to_cents(amount_float): return int(round(amount_float * 100))
    @staticmethod
    def to_dollars(amount_int): return amount_int / 100.0

    @staticmethod
    def distribute_amount(total_cents, weights):
        """核心算法：按权重分配金额，自动处理除不尽的余数 (v3.0功能回归)"""
        total_weight = sum(weights)
        if total_weight == 0: return [0] * len(weights)
        
        amounts = []
        current_sum = 0
        
        # 先按比例分配整数部分
        for w in weights:
            share = int((total_cents * w) / total_weight)
            amounts.append(share)
            current_sum += share
            
        # 处理余数 (Penny Allocation)
        remainder = total_cents - current_sum
        for i in range(remainder):
            amounts[i] += 1
            
        return amounts

    @staticmethod
    def simplify_debts(net_balances):
        """债务简化算法"""
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
# 🛠️ 3. 业务服务层
# ==========================================
class GroupService:
    @staticmethod
    def create_group(name, user_ids):
        try:
            grp = Group(id=str(uuid.uuid4()), name=name)
            session.add(grp)
            for uid in user_ids:
                session.add(GroupMember(group_id=grp.id, user_id=uid))
            session.commit()
            return True, "创建成功"
        except Exception as e:
            session.rollback()
            return False, str(e)

    @staticmethod
    def delete_group(group_id):
        grp = session.query(Group).filter_by(id=group_id).first()
        if grp:
            grp.is_deleted = True
            session.commit()
            return True
        return False

    @staticmethod
    def get_active_groups():
        return session.query(Group).filter_by(is_deleted=False).options(joinedload(Group.members).joinedload(GroupMember.user)).all()

class ExpenseService:
    @staticmethod
    def create_expense(desc, total_cents, group_id, created_by, category, payer_splits, ower_splits, custom_time=None):
        # 校验平衡
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
            session.rollback()
            return False, str(e)

    @staticmethod
    def create_repayment(payer_id, receiver_id, amount_cents, group_id, custom_time=None):
        payer_splits = {payer_id: amount_cents}
        ower_splits = {receiver_id: amount_cents}
        return ExpenseService.create_expense("还款", amount_cents, group_id, payer_id, "Repayment", payer_splits, ower_splits, custom_time)

    @staticmethod
    def delete_expense(exp_id):
        exp = session.query(Expense).filter_by(id=exp_id).first()
        if exp:
            exp.is_deleted = True
            session.commit()
            return True
        return False

    @staticmethod
    def get_balances(group_id):
        expenses = session.query(Expense).filter_by(group_id=group_id, is_deleted=False).all()
        balances = collections.defaultdict(int)
        for exp in expenses:
            for s in exp.splits:
                balances[s.user.username] += (s.paid_amount - s.owed_amount)
        return balances

    @staticmethod
    def get_activity(group_id):
        return session.query(Expense).filter_by(group_id=group_id, is_deleted=False).order_by(Expense.date.desc()).options(joinedload(Expense.creator)).all()

class UserService:
    @staticmethod
    def get_all(): return session.query(User).all()
    @staticmethod
    def create(name):
        if session.query(User).filter_by(username=name).first(): return False
        session.add(User(id=str(uuid.uuid4()), username=name))
        session.commit()
        return True

# ==========================================
# 🎨 4. 前端 UI (Streamlit)
# ==========================================
st.set_page_config(page_title="Splitwise Ultimate", page_icon="💸", layout="wide")
st.markdown("<style>.big-font {font-size:18px !important;}</style>", unsafe_allow_html=True)

if 'page' not in st.session_state: st.session_state.page = "dashboard"

# --- 侧边栏 ---
with st.sidebar:
    st.title("💸 聚会分账系统")
    st.caption("v5.0 终极融合版")
    
    with st.expander("👤 成员管理", expanded=True):
        new_u = st.text_input("添加新成员")
        if st.button("添加"):
            if new_u and UserService.create(new_u):
                st.success(f"{new_u} 已添加")
                st.rerun()

    st.divider()
    all_users = UserService.get_all()
    if not all_users:
        st.warning("请先添加成员")
        st.stop()
        
    current_u_name = st.selectbox("当前操作人", [u.username for u in all_users])
    current_u = next(u for u in all_users if u.username == current_u_name)
    
    st.divider()
    nav = st.radio("功能导航", ["📊 仪表盘 & 动态", "📝 记一笔 (支出)", "💸 还款 (结算)", "⚙️ 设置"])

# --- 1. 仪表盘 & 动态 ---
if nav == "📊 仪表盘 & 动态":
    st.header(f"👋 你好, {current_u.username}")
    groups = GroupService.get_active_groups()
    
    if not groups: st.info("暂无群组，请去设置创建")
    
    for grp in groups:
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
                bal = balances.get(current_u.username, 0)
                color = "green" if bal >= 0 else "red"
                txt = f"收回 {FinanceEngine.to_dollars(bal)}" if bal >= 0 else f"支付 {FinanceEngine.to_dollars(abs(bal))}"
                st.markdown(f":{color}[**需{txt}**]")

            st.divider()
            
            # B. 最近动态
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
                            # 显示分账详情
                            details = []
                            for s in exp.splits:
                                if s.paid_amount > 0: details.append(f"{s.user.username}付{FinanceEngine.to_dollars(s.paid_amount)}")
                                if s.owed_amount > 0: details.append(f"{s.user.username}耗{FinanceEngine.to_dollars(s.owed_amount)}")
                            st.caption(", ".join(details))
                        with col_b:
                            if st.button("🗑️ 删除", key=f"del_{exp.id}"):
                                ExpenseService.delete_expense(exp.id)
                                st.rerun()

# --- 2. 记一笔 (核心修复：恢复多种分账) ---
elif nav == "📝 记一笔 (支出)":
    st.header("📝 记录支出")
    groups = GroupService.get_active_groups()
    if not groups: st.stop()
    
    sel_grp = st.selectbox("选择群组", [g.name for g in groups])
    grp = next(g for g in groups if g.name == sel_grp)
    members = [m.user.username for m in grp.members]
    m_ids = {m.user.username: m.user.id for m in grp.members}
    
    with st.form("expense"):
        # 基本信息
        c1, c2, c3 = st.columns(3)
        desc = c1.text_input("消费内容", "聚餐")
        amt = c2.number_input("总金额", min_value=0.01, step=1.0)
        cat = c3.selectbox("分类", ["餐饮", "交通", "房租", "购物", "娱乐", "其他"])
        
        # 时间选择
        c4, c5 = st.columns(2)
        d_date = c4.date_input("日期", date.today())
        d_time = c5.time_input("时间", datetime.now().time())
        
        st.divider()
        
        # --- 1. 付款方 (支持多人) ---
        st.subheader("1. 谁付的钱?")
        pay_mode = st.radio("付款方式", ["单人垫付", "多人付款"], horizontal=True)
        payer_splits = {} 
        
        if pay_mode == "单人垫付":
            payer = st.selectbox("付款人", members, index=members.index(current_u.username) if current_u.username in members else 0)
            payer_splits[m_ids[payer]] = FinanceEngine.to_cents(amt)
        else:
            st.caption("输入每个人支付的金额：")
            cols = st.columns(len(members))
            for i, m in enumerate(members):
                val = cols[i].number_input(f"{m} 付了", min_value=0.0, step=1.0, key=f"pay_{m}")
                if val > 0: payer_splits[m_ids[m]] = FinanceEngine.to_cents(val)

        st.divider()

        # --- 2. 分摊方 (支持4种模式 - 核心回归) ---
        st.subheader("2. 怎么分?")
        split_method = st.radio("分账模式", ["🏁 均分 (Equal)", "🔢 按份数 (Shares)", "💯 按百分比 (%)", "💵 具体金额"], horizontal=True)
        
        ower_splits = {}
        total_cents = FinanceEngine.to_cents(amt)
        
        if split_method == "🏁 均分 (Equal)":
            involved = st.multiselect("选择参与人", members, default=members)
            if involved:
                weights = [1] * len(involved)
                amounts = FinanceEngine.distribute_amount(total_cents, weights)
                for i, m in enumerate(involved):
                    ower_splits[m_ids[m]] = amounts[i]
                    
        elif split_method == "🔢 按份数 (Shares)":
            st.info("例如：A 吃了 2 份，B 吃了 1 份")
            cols = st.columns(len(members))
            weights = []
            active_members = []
            for i, m in enumerate(members):
                w = cols[i].number_input(f"{m} 的份数", min_value=0, step=1, value=1, key=f"share_{m}")
                weights.append(w)
                active_members.append(m)
            
            if sum(weights) > 0:
                amounts = FinanceEngine.distribute_amount(total_cents, weights)
                for i, m in enumerate(active_members):
                    if amounts[i] > 0: ower_splits[m_ids[m]] = amounts[i]

        elif split_method == "💯 按百分比 (%)":
            cols = st.columns(len(members))
            pcts = []
            for i, m in enumerate(members):
                p = cols[i].number_input(f"{m} (%)", min_value=0.0, max_value=100.0, step=5.0, key=f"pct_{m}")
                pcts.append(p)
            
            if abs(sum(pcts) - 100.0) < 0.01:
                weights = [int(p*100) for p in pcts] 
                amounts = FinanceEngine.distribute_amount(total_cents, weights)
                for i, m in enumerate(members):
                    if amounts[i] > 0: ower_splits[m_ids[m]] = amounts[i]
            else:
                st.error(f"当前总和: {sum(pcts)}%，必须等于 100%")

        elif split_method == "💵 具体金额":
            st.info("💡 请直接输入每个人该付多少钱：")
            # 优化：强制只用 3 列，防止人多时输入框被挤压消失
            cols = st.columns(3) 
            input_sum = 0
            
            for i, m in enumerate(members):
                # 自动换行逻辑：0,1,2 在第一行，3,4,5 在第二行...
                with cols[i % 3]:
                    val = st.number_input(f"{m} 应付", min_value=0.0, step=1.0, key=f"exact_{m}")
                    c = FinanceEngine.to_cents(val)
                    if c > 0:
                        ower_splits[m_ids[m]] = c
                        input_sum += c
            
            # 实时帮你可以算差额，防止算错
            diff = total_cents - input_sum
            if diff != 0:
                if diff > 0:
                    st.warning(f"⚠️ 还有 {FinanceEngine.to_dollars(diff)} 元未分配")
                else:
                    st.error(f"🚫 多分了 {FinanceEngine.to_dollars(abs(diff))} 元")
            else:
                st.success("✅ 金额完美匹配")
            st.caption("手动输入应付金额")
            cols = st.columns(len(members))
            input_sum = 0
            for i, m in enumerate(members):
                val = cols[i].number_input(f"{m} 应付", min_value=0.0, step=1.0, key=f"exact_{m}")
                c = FinanceEngine.to_cents(val)
                if c > 0:
                    ower_splits[m_ids[m]] = c
                    input_sum += c
            if input_sum != total_cents:
                st.error(f"还有 {FinanceEngine.to_dollars(total_cents - input_sum)} 未分配")
        
        # --- 3. 提交 ---
        if st.form_submit_button("✅ 确认记账", type="primary"):
            if not payer_splits:
                st.error("必须有付款人")
            elif not ower_splits:
                st.error("必须有分摊人")
            else:
                final_dt = datetime.combine(d_date, d_time)
                success, msg = ExpenseService.create_expense(desc, total_cents, grp.id, current_u.id, cat, payer_splits, ower_splits, final_dt)
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
    groups = GroupService.get_active_groups()
    if not groups: st.stop()
    
    sel_grp_s = st.selectbox("选择群组", [g.name for g in groups], key="settle_grp")
    grp_s = next(g for g in groups if g.name == sel_grp_s)
    members_s = [m.user.username for m in grp_s.members]
    m_ids_s = {m.user.username: m.user.id for m in grp_s.members}
    
    c1, c2, c3 = st.columns(3)
    payer_s = c1.selectbox("付款人 (谁还钱)", members_s, index=0)
    receiver_s = c2.selectbox("收款人 (还给谁)", members_s, index=1 if len(members_s)>1 else 0)
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
elif nav == "⚙️ 设置":
    st.subheader("创建新群组")
    n_grp = st.text_input("群名")
    others = [u.username for u in all_users if u.username != current_u.username]
    invites = st.multiselect("拉人进群", others)
    if st.button("建群"):
        if n_grp:
            uids = [u.id for u in all_users if u.username in invites + [current_u.username]]
            GroupService.create_group(n_grp, uids)
            st.success("成功")
            st.rerun()
            
    st.divider()
    st.subheader("删除群组")
    # ✅ 修复：正确获取 groups 变量
    groups = GroupService.get_active_groups()
    if groups:
        del_g = st.selectbox("选择删除", [g.name for g in groups])
        if st.button("删除该群"):
            t_g = next(g for g in groups if g.name == del_g)
            GroupService.delete_group(t_g.id)
            st.rerun()
    else:
        st.info("没有可删除的群组")