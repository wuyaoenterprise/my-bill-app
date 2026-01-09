import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Boolean, DateTime, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, joinedload
from datetime import datetime
import uuid
import collections
import heapq

# ==========================================
# 🏗️ 1. 数据库定义
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
    members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan")

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
    splits = relationship("Split", back_populates="expense", cascade="all, delete-orphan")
    creator = relationship("User")

class Split(Base):
    __tablename__ = 'splits'
    id = Column(Integer, primary_key=True)
    expense_id = Column(String, ForeignKey('expenses.id'))
    user_id = Column(String, ForeignKey('users.id'))
    paid_amount = Column(BigInteger, default=0)
    owed_amount = Column(BigInteger, default=0)
    user = relationship("User")

# --- 数据库连接 (带缓存) ---
@st.cache_resource
def init_db():
    db_url = st.secrets.get("DATABASE_URL", "sqlite:///splitwise_pro.db")
    engine = create_engine(db_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)

SessionLocal = init_db()

# ==========================================
# 🧠 2. 核心逻辑引擎
# ==========================================
class FinanceEngine:
    @staticmethod
    def to_cents(amt): return int(round(float(amt) * 100))
    @staticmethod
    def to_dollars(cents): return float(cents) / 100.0
    
    @staticmethod
    def distribute(total, weights):
        tw = sum(weights)
        if tw == 0: return [0] * len(weights)
        shares = [int(total * w / tw) for w in weights]
        rem = total - sum(shares)
        for i in range(rem): shares[i] += 1
        return shares

    @staticmethod
    def simplify(balances):
        debtors = [] # 欠钱
        creditors = [] # 收钱
        for p, a in balances.items():
            if a < -1: heapq.heappush(debtors, (a, p))
            elif a > 1: heapq.heappush(creditors, (-a, p))
        
        txs = []
        while debtors and creditors:
            d_amt, d_p = heapq.heappop(debtors)
            c_amt, c_p = heapq.heappop(creditors)
            m = min(-d_amt, -c_amt)
            txs.append({"from": d_p, "to": c_p, "amount": m})
            if d_amt + m < -1: heapq.heappush(debtors, (d_amt + m, d_p))
            if c_amt + m < -1: heapq.heappush(creditors, (c_amt + m, c_p))
        return txs

# ==========================================
# 🎨 3. UI 界面 (零延迟版)
# ==========================================
st.set_page_config(page_title="SplitPro", layout="wide")

# 侧边栏：操作人管理
with st.sidebar:
    st.title("💸 聚会分账系统")
    with SessionLocal() as s:
        all_u = s.query(User).all()
        if not all_u:
            new_u = st.text_input("初始化成员")
            if st.button("确定"):
                s.add(User(id=str(uuid.uuid4()), username=new_u))
                s.commit()
                st.rerun()
            st.stop()
        curr_u_name = st.selectbox("当前操作人", [u.username for u in all_u])
        curr_u = next(u for u in all_u if u.username == curr_u_name)
    
    st.divider()
    # 模拟 app.py 的 Tab 导航，这是减少延迟的关键
    nav = st.radio("功能导航", ["📊 仪表盘 & 历史", "📝 记一笔", "💸 还款结算", "🤝 群组设置"])

# --- 缓存数据读取 ---
@st.cache_data(ttl=60)
def get_data_cache():
    with SessionLocal() as s:
        grps = s.query(Group).filter_by(is_deleted=False).options(joinedload(Group.members).joinedload(GroupMember.user)).all()
        # 预加载所有未删除的支出
        exps = s.query(Expense).filter_by(is_deleted=False).options(joinedload(Expense.splits).joinedload(Split.user)).order_by(Expense.date.desc()).all()
        return grps, exps

grps, all_exps = get_data_cache()

# --- 1. 仪表盘 ---
if nav == "📊 仪表盘 & 历史":
    st.header(f"👋 你好, {curr_u.username}")
    if not grps: st.info("请先去「群组设置」创建群组")
    
    for g in grps:
        with st.container(border=True):
            st.subheader(f"📂 {g.name}")
            # 计算余额
            g_exps = [e for e in all_exps if e.group_id == g.id]
            bals = collections.defaultdict(int)
            for e in g_exps:
                for sp in e.splits:
                    bals[sp.user.username] += (sp.paid_amount - sp.owed_amount)
            
            c1, c2 = st.columns(2)
            with c1:
                st.caption("💰 待结算")
                txs = FinanceEngine.simplify(bals)
                if not txs: st.write("账目已平")
                for t in txs:
                    st.markdown(f"**{t['from']}** ➡ **{t['to']}** : :green[RM {FinanceEngine.to_dollars(t['amount'])}]")
            with c2:
                my_b = bals.get(curr_u.username, 0)
                st.metric("我的余额", f"RM {FinanceEngine.to_dollars(my_b)}", delta="收回" if my_b >=0 else "支付")
            
            # 历史记录列表 (带删除)
            with st.expander("🕒 查看历史记录"):
                for e in g_exps:
                    col_e1, col_e2, col_e3 = st.columns([3,1,1])
                    col_e1.write(f"{e.date.strftime('%m-%d')} **{e.description}** ({e.category})")
                    col_e2.write(f"RM {FinanceEngine.to_dollars(e.amount)}")
                    if col_e3.button("🗑️", key=f"del_{e.id}"):
                        with SessionLocal() as s:
                            s.query(Expense).filter_by(id=e.id).update({"is_deleted": True})
                            s.commit()
                            st.cache_data.clear()
                            st.rerun()

# --- 2. 记一笔 (修复模式跳不出) ---
elif nav == "📝 记一笔":
    st.header("📝 记录支出")
    if not grps: st.stop()
    
    sel_g = st.selectbox("群组", [g.name for g in grps])
    g_obj = next(g for g in grps if g.name == sel_g)
    m_names = [m.user.username for m in g_obj.members]
    m_map = {m.user.username: m.user.id for m in g_obj.members}
    
    # 基础信息
    c_b1, c_b2, c_b3 = st.columns(3)
    desc = c_b1.text_input("内容", "晚餐")
    amt_f = c_b2.number_input("金额", min_value=0.0, step=0.1)
    cat = c_b3.selectbox("分类", ["餐饮", "交通", "购物", "其他"])
    total_c = FinanceEngine.to_cents(amt_f)
    
    st.write("---")
    # 付款人逻辑
    st.subheader("1. 谁付款？")
    p_names = st.multiselect("选择付款人 (支持多人)", m_names, default=[curr_u_name])
    p_splits = {}
    if len(p_names) == 1:
        p_splits[m_map[p_names[0]]] = total_c
    else:
        p_cols = st.columns(len(p_names))
        for i, pn in enumerate(p_names):
            v = p_cols[i].number_input(f"{pn}付", min_value=0.0, key=f"p_{pn}")
            p_splits[m_map[pn]] = FinanceEngine.to_cents(v)

    st.write("---")
    # 分账逻辑 - 实时渲染
    st.subheader("2. 怎么分？")
    s_mode = st.radio("分账模式", ["均分", "按份数", "具体金额"], horizontal=True)
    o_splits = {}
    
    if s_mode == "均分":
        targets = st.multiselect("参与人", m_names, default=m_names)
        if targets:
            amts = FinanceEngine.distribute(total_c, [1]*len(targets))
            for i, tn in enumerate(targets): o_splits[m_map[tn]] = amts[i]
    elif s_mode == "按份数":
        s_cols = st.columns(len(m_names))
        ws = [s_cols[i].number_input(f"{mn}(份)", 0, 10, 1, key=f"s_{mn}") for i, mn in enumerate(m_names)]
        amts = FinanceEngine.distribute(total_c, ws)
        for i, mn in enumerate(m_names): o_splits[m_map[mn]] = amts[i]
    elif s_mode == "具体金额":
        e_cols = st.columns(len(m_names))
        cur_sum = 0
        for i, mn in enumerate(m_names):
            v = e_cols[i].number_input(f"{mn}(RM)", min_value=0.0, key=f"e_{mn}")
            o_splits[m_map[mn]] = FinanceEngine.to_cents(v)
            cur_sum += o_splits[m_map[mn]]
        if cur_sum != total_c: st.warning(f"不平: 差 RM {FinanceEngine.to_dollars(total_c-cur_sum)}")

    if st.button("✅ 记账", type="primary", use_container_width=True):
        with SessionLocal() as s:
            eid = str(uuid.uuid4())
            s.add(Expense(id=eid, group_id=g_obj.id, created_by=curr_u.id, description=desc, amount=total_c, category=cat))
            for uid in set(list(p_splits.keys()) + list(o_splits.keys())):
                s.add(Split(expense_id=eid, user_id=uid, paid_amount=p_splits.get(uid,0), owed_amount=o_splits.get(uid,0)))
            s.commit()
            st.cache_data.clear()
            st.rerun()

# --- 3. 还款功能 (核心回归) ---
elif nav == "💸 还款结算":
    st.header("💸 记录还款")
    sel_g = st.selectbox("群组", [g.name for g in grps])
    g_obj = next(g for g in grps if g.name == sel_g)
    m_names = [m.user.username for m in g_obj.members]
    m_map = {m.user.username: m.user.id for m in g_obj.members}
    
    c1, c2, c3 = st.columns(3)
    payer = c1.selectbox("谁给钱", m_names)
    receiver = c2.selectbox("谁收钱", [n for n in m_names if n != payer])
    amt = c3.number_input("还款金额", min_value=0.1)
    
    if st.button("确认还款", type="primary"):
        cents = FinanceEngine.to_cents(amt)
        with SessionLocal() as s:
            eid = str(uuid.uuid4())
            # 还款本质：Payer 付了钱，Receiver 消耗了钱
            s.add(Expense(id=eid, group_id=g_obj.id, created_by=m_map[payer], description=f"还款: {payer} -> {receiver}", amount=cents, category="还款"))
            s.add(Split(expense_id=eid, user_id=m_map[payer], paid_amount=cents, owed_amount=0))
            s.add(Split(expense_id=eid, user_id=m_map[receiver], paid_amount=0, owed_amount=cents))
            s.commit()
            st.cache_data.clear()
            st.success("还款记录已保存")
            st.rerun()

# --- 4. 群组设置 ---
elif nav == "🤝 群组设置":
    st.subheader("创建新群组")
    g_name = st.text_input("群名")
    with SessionLocal() as s:
        all_users = s.query(User).all()
        selected = st.multiselect("选择成员", [u.username for u in all_users], default=[curr_u_name])
        if st.button("创建"):
            gid = str(uuid.uuid4())
            s.add(Group(id=gid, name=g_name))
            for name in selected:
                uid = next(u.id for u in all_users if u.username == name)
                s.add(GroupMember(group_id=gid, user_id=uid))
            s.commit()
            st.cache_data.clear()
            st.rerun()
