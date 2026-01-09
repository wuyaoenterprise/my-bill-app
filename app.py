import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Boolean, DateTime, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, joinedload
from datetime import datetime
import uuid
import collections
import heapq

# ==========================================
# 🏗️ 1. 模型修复 (解决 InvalidRequestError)
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
    # 确保 back_populates 对应正确
    members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan")

class GroupMember(Base):
    __tablename__ = 'group_members'
    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(String, ForeignKey('groups.id'))
    user_id = Column(String, ForeignKey('users.id'))
    # 【修复点】必须声明 back_populates="members"
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
    expense = relationship("Expense", back_populates="splits")
    user = relationship("User")

# --- 数据库初始化 ---
@st.cache_resource
def get_db_session():
    db_url = st.secrets.get("DATABASE_URL", "sqlite:///splitwise_pro.db")
    if db_url.startswith("postgres://"): db_url = db_url.replace("postgres://", "postgresql://", 1)
    engine = create_engine(db_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)

Session = get_db_session()

# ==========================================
# 🧠 2. 财务算法
# ==========================================
class FinCore:
    @staticmethod
    def to_cents(f): return int(round(float(f) * 100))
    @staticmethod
    def to_rm(c): return float(c) / 100.0
    
    @staticmethod
    def distribute(total, weights):
        tw = sum(weights)
        if tw == 0: return [0] * len(weights)
        res = [int(total * w / tw) for w in weights]
        for i in range(total - sum(res)): res[i] += 1
        return res

    @staticmethod
    def get_txs(bals):
        d, c = [], []
        for p, a in bals.items():
            if a < -1: heapq.heappush(d, (a, p))
            elif a > 1: heapq.heappush(c, (-a, p))
        res = []
        while d and c:
            da, dp = heapq.heappop(d)
            ca, cp = heapq.heappop(c)
            m = min(-da, -ca)
            res.append({"f": dp, "t": cp, "a": m})
            if da + m < -1: heapq.heappush(d, (da+m, dp))
            if ca + m < -1: heapq.heappush(c, (ca+m, cp))
        return res

# ==========================================
# 🎨 3. UI 交互 (0延迟架构)
# ==========================================
st.set_page_config(page_title="SplitPro", layout="wide")

# --- 侧边栏 ---
with st.sidebar:
    st.title("💸 聚会分账系统")
    with Session() as s:
        all_u = s.query(User).all()
        if not all_u:
            name = st.text_input("初始用户")
            if st.button("创建"):
                s.add(User(id=str(uuid.uuid4()), username=name)); s.commit(); st.rerun()
            st.stop()
        curr_u_name = st.selectbox("当前操作人", [u.username for u in all_u])
        curr_u = next(u for u in all_u if u.username == curr_u_name)
    st.divider()
    nav = st.radio("功能", ["📊 统计中心", "📝 记录支出", "💸 还款结算", "⚙️ 群组设置"])

# --- 数据预加载 (缓存) ---
@st.cache_data(ttl=300)
def load_data():
    with Session() as s:
        grps = s.query(Group).filter_by(is_deleted=False).options(joinedload(Group.members).joinedload(GroupMember.user)).all()
        exps = s.query(Expense).filter_by(is_deleted=False).options(joinedload(Expense.splits).joinedload(Split.user)).order_by(Expense.date.desc()).all()
        return grps, exps

grps, exps_all = load_data()

# --- 1. 统计中心 ---
if nav == "📊 统计中心":
    st.header(f"你好, {curr_u.username}")
    for g in grps:
        with st.container(border=True):
            st.subheader(f"📂 {g.name}")
            g_exps = [e for e in exps_all if e.group_id == g.id]
            bals = collections.defaultdict(int)
            for e in g_exps:
                for sp in e.splits: bals[sp.user.username] += (sp.paid_amount - sp.owed_amount)
            
            c1, c2 = st.columns(2)
            with c1:
                st.caption("待结算金额")
                txs = FinCore.get_txs(bals)
                for t in txs: st.write(f"**{t['f']}** ➡ **{t['t']}**: :green[RM {FinCore.to_rm(t['a'])}]")
            with c2:
                my = bals.get(curr_u.username, 0)
                st.metric("我的状况", f"RM {FinCore.to_rm(my)}", "需收回" if my>=0 else "需支付")

            with st.expander("🕒 历史账单"):
                for e in g_exps:
                    ec1, ec2, ec3 = st.columns([3,1,1])
                    ec1.write(f"{e.date.strftime('%m-%d')} {e.description}")
                    ec2.write(f"RM {FinCore.to_rm(e.amount)}")
                    if ec3.button("🗑️", key=f"del_{e.id}"):
                        with Session() as s:
                            s.query(Expense).filter_by(id=e.id).update({"is_deleted": True}); s.commit()
                            st.cache_data.clear(); st.rerun()

# --- 2. 记录支出 ---
elif nav == "📝 记录支出":
    st.header("新支出")
    if not grps: st.stop()
    sel_g = st.selectbox("群组", [g.name for g in grps])
    g_obj = next(g for g in grps if g.name == sel_g)
    m_names = [m.user.username for m in g_obj.members]
    m_ids = {m.user.username: m.user.id for m in g_obj.members}

    c1, c2 = st.columns(2)
    desc = c1.text_input("内容", "聚餐")
    amt = c2.number_input("总金额", 0.0, step=0.1)
    total_c = FinCore.to_cents(amt)

    st.divider()
    st.subheader("付款人")
    p_u = st.selectbox("谁付的", m_names, index=m_names.index(curr_u_name) if curr_u_name in m_names else 0)
    
    st.divider()
    st.subheader("分账模式")
    mode = st.radio("模式", ["均分", "按份数", "具体金额"], horizontal=True)
    o_splits = {}
    
    # 动态显示分账输入 (不使用 form)
    if mode == "均分":
        targets = st.multiselect("参与人", m_names, default=m_names)
        if targets:
            amts = FinCore.distribute(total_c, [1]*len(targets))
            for i, n in enumerate(targets): o_splits[m_ids[n]] = amts[i]
    elif mode == "按份数":
        cols = st.columns(len(m_names))
        ws = [cols[i].number_input(f"{n}(份)", 0, 10, 1, key=f"ws_{n}") for i, n in enumerate(m_names)]
        amts = FinCore.distribute(total_c, ws)
        for i, n in enumerate(m_names): o_splits[m_ids[n]] = amts[i]
    elif mode == "具体金额":
        cols = st.columns(len(m_names))
        cur = 0
        for i, n in enumerate(m_names):
            v = cols[i].number_input(f"{n}(RM)", 0.0, key=f"ex_{n}")
            o_splits[m_ids[n]] = FinCore.to_cents(v); cur += o_splits[m_ids[n]]
        if cur != total_c: st.warning(f"差额: RM {FinCore.to_rm(total_c-cur)}")

    if st.button("✅ 确认记账", use_container_width=True, type="primary"):
        with Session() as s:
            eid = str(uuid.uuid4())
            s.add(Expense(id=eid, group_id=g_obj.id, created_by=curr_u.id, description=desc, amount=total_c))
            s.add(Split(expense_id=eid, user_id=m_ids[p_u], paid_amount=total_c, owed_amount=0)) # 付款
            for uid, val in o_splits.items():
                existing = s.query(Split).filter_by(expense_id=eid, user_id=uid).first()
                if existing: existing.owed_amount = val
                else: s.add(Split(expense_id=eid, user_id=uid, paid_amount=0, owed_amount=val))
            s.commit(); st.cache_data.clear(); st.rerun()

# --- 3. 还款结算 (核心功能) ---
elif nav == "💸 还款结算":
    st.header("记录还款")
    if not grps: st.stop()
    sel_g = st.selectbox("选择群组", [g.name for g in grps])
    g_obj = next(g for g in grps if g.name == sel_g)
    m_names = [m.user.username for m in g_obj.members]
    m_ids = {m.user.username: m.user.id for m in g_obj.members}
    
    col1, col2, col3 = st.columns(3)
    p_from = col1.selectbox("付款人 (还钱者)", m_names)
    p_to = col2.selectbox("收款人 (收钱者)", [n for n in m_names if n != p_from])
    p_amt = col3.number_input("还款金额", 0.1)

    if st.button("确认还款", type="primary"):
        cents = FinCore.to_cents(p_amt)
        with Session() as s:
            eid = str(uuid.uuid4())
            # 还款记录：付款者付钱，收款者消耗
            s.add(Expense(id=eid, group_id=g_obj.id, created_by=m_ids[p_from], description=f"还款: {p_from}->{p_to}", amount=cents, category="还款"))
            s.add(Split(expense_id=eid, user_id=m_ids[p_from], paid_amount=cents, owed_amount=0))
            s.add(Split(expense_id=eid, user_id=m_ids[p_to], paid_amount=0, owed_amount=cents))
            s.commit(); st.cache_data.clear(); st.success("还款已记录"); st.rerun()

# --- 4. 设置 ---
elif nav == "⚙️ 群组设置":
    st.subheader("创建群组")
    g_name = st.text_input("群名")
    with Session() as s:
        all_users = s.query(User).all()
        selected = st.multiselect("成员", [u.username for u in all_users], default=[curr_u_name])
        if st.button("创建"):
            gid = str(uuid.uuid4())
            s.add(Group(id=gid, name=g_name))
            for n in selected:
                uid = next(u.id for u in all_users if u.username == n)
                s.add(GroupMember(group_id=gid, user_id=uid))
            s.commit(); st.cache_data.clear(); st.rerun()
