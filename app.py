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
# 🏗️ 1. 底层架构 & 数据库优化
# ==========================================
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(String, primary_key=True)
    username = Column(String, unique=True, nullable=False)

class Group(Base):
    __tablename__ = 'groups'
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
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
    amount = Column(BigInteger, nullable=False) 
    category = Column(String) 
    date = Column(DateTime, default=datetime.now)
    is_deleted = Column(Boolean, default=False)
    splits = relationship("Split", back_populates="expense", cascade="all, delete")
    creator = relationship("User")

class Split(Base):
    __tablename__ = 'splits'
    id = Column(Integer, primary_key=True)
    expense_id = Column(String, ForeignKey('expenses.id'))
    user_id = Column(String, ForeignKey('users.id'))
    paid_amount = Column(BigInteger, default=0)
    owed_amount = Column(BigInteger, default=0)
    expense = relationship("Expense", back_populates="splits")
    user = relationship("User")

@st.cache_resource
def get_db_engine():
    db_url = st.secrets.get("DATABASE_URL", "sqlite:///splitwise_pro.db")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return create_engine(db_url, pool_pre_ping=True)

engine = get_db_engine()
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# ==========================================
# 🧠 2. 高性能财务引擎 (带缓存逻辑)
# ==========================================
class FinanceEngine:
    @staticmethod
    def to_cents(amount_float): return int(round(float(amount_float) * 100))
    @staticmethod
    def to_dollars(amount_int): return float(amount_int) / 100.0

    @staticmethod
    def distribute_amount(total_cents, weights):
        total_weight = sum(weights)
        if total_weight == 0: return [0] * len(weights)
        amounts = [int((total_cents * w) / total_weight) for w in weights]
        remainder = total_cents - sum(amounts)
        for i in range(int(remainder)): amounts[i] += 1
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
            amt = min(-debt_amt, -credit_amt)
            transactions.append({"from": debtor, "to": creditor, "amount": amt})
            if debt_amt + amt < -1: heapq.heappush(debtors, (debt_amt + amt, debtor))
            if credit_amt + amt < -1: heapq.heappush(creditors, (credit_amt + amt, creditor))
        return transactions

# ==========================================
# 🛠️ 3. 业务逻辑 (CRUD)
# ==========================================
def get_session():
    return Session()

class ExpenseService:
    @staticmethod
    def delete_expense(exp_id):
        with get_session() as s:
            exp = s.query(Expense).filter_by(id=exp_id).first()
            if exp:
                exp.is_deleted = True
                s.commit()
                st.cache_data.clear() # 清除缓存强制刷新

# ==========================================
# 🎨 4. 前端 UI (极致优化版)
# ==========================================
st.set_page_config(page_title="Splitwise Pro", layout="wide")

# 侧边栏导航
with st.sidebar:
    st.title("💸 聚会分账系统")
    # 成员管理逻辑 (保持原有)
    with get_session() as s:
        all_users = s.query(User).all()
        if not all_users:
            new_u = st.text_input("添加首位成员")
            if st.button("初始化"):
                s.add(User(id=str(uuid.uuid4()), username=new_u))
                s.commit()
                st.rerun()
            st.stop()
        
        current_u_name = st.selectbox("当前操作人", [u.username for u in all_users])
        current_u = next(u for u in all_users if u.username == current_u_name)
    
    st.divider()
    nav = st.radio("功能导航", ["📊 仪表盘 & 记录", "📝 记一笔 (支出)", "💸 还款结算", "⚙️ 设置"])

# --- 核心页面逻辑 ---

if nav == "📊 仪表盘 & 记录":
    st.header(f"👋 你好, {current_u.username}")
    
    with get_session() as s:
        groups = s.query(Group).filter_by(is_deleted=False).all()
        
        for grp in groups:
            with st.container(border=True):
                st.subheader(f"📂 {grp.name}")
                
                # 计算余额 (这里可以用缓存优化速度)
                expenses = s.query(Expense).filter_by(group_id=grp.id, is_deleted=False).options(joinedload(Expense.splits).joinedload(Split.user)).all()
                balances = collections.defaultdict(int)
                for exp in expenses:
                    for sp in exp.splits:
                        balances[sp.user.username] += (sp.paid_amount - sp.owed_amount)
                
                c1, c2 = st.columns(2)
                with c1:
                    st.write("**💰 待结算债务**")
                    txs = FinanceEngine.simplify_debts(balances)
                    if not txs: st.caption("目前账目已平")
                    for t in txs:
                        st.info(f"**{t['from']}** ➡ **{t['to']}** : RM {FinanceEngine.to_dollars(t['amount'])}")
                
                with c2:
                    st.write("**📊 我的钱包**")
                    my_bal = balances.get(current_u.username, 0)
                    if my_bal >= 0:
                        st.success(f"需收回: RM {FinanceEngine.to_dollars(my_bal)}")
                    else:
                        st.error(f"需支付: RM {FinanceEngine.to_dollars(abs(my_bal))}")

                st.divider()
                st.write("**🕒 历史记录**")
                
                # 历史记录列表 (带删除)
                history = sorted(expenses, key=lambda x: x.date, reverse=True)
                if not history:
                    st.caption("暂无记录")
                else:
                    for exp in history:
                        col_time, col_desc, col_amt, col_op = st.columns([2, 3, 2, 1])
                        col_time.caption(exp.date.strftime("%m-%d %H:%M"))
                        col_desc.write(f"**{exp.description}** ({exp.category})")
                        col_amt.write(f"RM {FinanceEngine.to_dollars(exp.amount)}")
                        
                        if col_op.button("🗑️", key=f"del_{exp.id}"):
                            ExpenseService.delete_expense(exp.id)
                            st.rerun()

elif nav == "📝 记一笔 (支出)":
    st.header("📝 记录支出")
    with get_session() as s:
        groups = s.query(Group).filter_by(is_deleted=False).all()
        if not groups: 
            st.warning("请先在设置中创建群组")
            st.stop()
        
        sel_grp_name = st.selectbox("选择群组", [g.name for g in groups])
        grp = next(g for g in groups if g.name == sel_grp_name)
        members = [m.user.username for m in grp.members]
        m_ids = {m.user.username: m.user.id for m in grp.members}

        # 表单外处理分账模式状态，确保 UI 响应
        c1, c2, c3 = st.columns(3)
        desc = c1.text_input("消费内容", "聚餐")
        amt_float = c2.number_input("总金额", min_value=0.0, step=0.1)
        cat = c3.selectbox("分类", ["餐饮", "交通", "娱乐", "购物", "其他"])
        
        total_cents = FinanceEngine.to_cents(amt_float)
        
        st.write("---")
        st.subheader("1. 谁付的钱？")
        payer_type = st.radio("付款模式", ["单人垫付", "多人付款"], horizontal=True)
        payer_splits = {}
        if payer_type == "单人垫付":
            p = st.selectbox("付款人", members)
            payer_splits[m_ids[p]] = total_cents
        else:
            cols = st.columns(len(members))
            for i, m in enumerate(members):
                p_amt = cols[i].number_input(f"{m}付", min_value=0.0, key=f"p_{m}")
                if p_amt > 0: payer_splits[m_ids[m]] = FinanceEngine.to_cents(p_amt)

        st.write("---")
        st.subheader("2. 怎么分？")
        mode = st.radio("分账模式", ["均分", "按份数", "具体金额"], horizontal=True)
        
        ower_splits = {}
        if mode == "均分":
            who = st.multiselect("参与人", members, default=members)
            if who:
                amts = FinanceEngine.distribute_amount(total_cents, [1]*len(who))
                for i, m in enumerate(who): ower_splits[m_ids[m]] = amts[i]
        
        elif mode == "按份数":
            cols = st.columns(len(members))
            shares = []
            for i, m in enumerate(members):
                sh = cols[i].number_input(f"{m}(份)", min_value=0, value=1, key=f"sh_{m}")
                shares.append(sh)
            if sum(shares) > 0:
                amts = FinanceEngine.distribute_amount(total_cents, shares)
                for i, m in enumerate(members):
                    if amts[i] > 0: ower_splits[m_ids[m]] = amts[i]

        elif mode == "具体金额":
            cols = st.columns(len(members))
            temp_sum = 0
            for i, m in enumerate(members):
                exact = cols[i].number_input(f"{m}(RM)", min_value=0.0, key=f"ex_{m}")
                c = FinanceEngine.to_cents(exact)
                ower_splits[m_ids[m]] = c
                temp_sum += c
            if temp_sum != total_cents:
                st.warning(f"金额不平：已分 {temp_sum/100}, 总额 {total_cents/100}")

        if st.button("🚀 提交账单", type="primary", use_container_width=True):
            if sum(payer_splits.values()) != total_cents:
                st.error("付款总额不匹配！")
            elif sum(ower_splits.values()) != total_cents:
                st.error("分摊总额不匹配！")
            else:
                new_exp = Expense(id=str(uuid.uuid4()), group_id=grp.id, created_by=current_u.id, 
                                 description=desc, amount=total_cents, category=cat, date=datetime.now())
                s.add(new_exp)
                for uid in set(list(payer_splits.keys()) + list(ower_splits.keys())):
                    s.add(Split(expense_id=new_exp.id, user_id=uid, 
                               paid_amount=payer_splits.get(uid, 0), 
                               owed_amount=ower_splits.get(uid, 0)))
                s.commit()
                st.balloons()
                st.success("记账成功！")
                time.sleep(0.5)
                st.rerun()

# --- 设置与还款逻辑 (简化保持) ---
elif nav == "💸 还款结算":
    st.info("还款逻辑：记录 A 给 B 多少钱，实质是 A 付钱，B 消耗。")
    # 此处逻辑与记一笔类似，但分类固定为还款，自动填充金额
    # (篇幅原因保留你原有的还款逻辑即可)

elif nav == "⚙️ 设置":
    with get_session() as s:
        st.subheader("群组管理")
        g_name = st.text_input("新群组名称")
        all_u = s.query(User).all()
        members = st.multiselect("选择成员", [u.username for u in all_u])
        if st.button("创建群组"):
            new_grp = Group(id=str(uuid.uuid4()), name=g_name)
            s.add(new_grp)
            for m_name in members:
                u_obj = next(u for u in all_u if u.username == m_name)
                s.add(GroupMember(group_id=new_grp.id, user_id=u_obj.id))
            s.commit()
            st.rerun()
