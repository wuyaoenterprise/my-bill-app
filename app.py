import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Boolean, DateTime, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, joinedload
from datetime import datetime, date
import uuid
import collections
import heapq
import time

# ==========================================
# 🏗️ 1. 数据库底层 (保持稳定)
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
    id = Column(Integer, primary_key=True, autoincrement=True)
    expense_id = Column(String, ForeignKey('expenses.id'))
    user_id = Column(String, ForeignKey('users.id'))
    paid_amount = Column(BigInteger, default=0)
    owed_amount = Column(BigInteger, default=0)
    expense = relationship("Expense", back_populates="splits")
    user = relationship("User")

# --- 连接池优化 ---
@st.cache_resource
def get_engine():
    db_url = st.secrets.get("DATABASE_URL", "sqlite:///splitwise_pro.db")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return create_engine(db_url, pool_pre_ping=True)

engine = get_engine()
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# ==========================================
# 🧠 2. 核心逻辑 (带缓存优化)
# ==========================================

@st.cache_data(ttl=600)
def fetch_balances(group_id):
    """缓存余额计算，减少数据库压力"""
    with Session() as s:
        expenses = s.query(Expense).filter_by(group_id=group_id, is_deleted=False).options(joinedload(Expense.splits).joinedload(Split.user)).all()
        balances = collections.defaultdict(int)
        for exp in expenses:
            for sp in exp.splits:
                balances[sp.user.username] += (sp.paid_amount - sp.owed_amount)
        return dict(balances)

@st.cache_data(ttl=600)
def fetch_groups():
    """缓存群组列表"""
    with Session() as s:
        return s.query(Group).filter_by(is_deleted=False).options(joinedload(Group.members).joinedload(GroupMember.user)).all()

class FinanceUtils:
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
# 🎨 3. UI 界面 (响应式优化)
# ==========================================
st.set_page_config(page_title="SplitPro", layout="wide")

# 侧边栏
with st.sidebar:
    st.title("💸 聚会分账系统")
    with Session() as s:
        all_users = s.query(User).all()
        if not all_users:
            new_u = st.text_input("初始化成员")
            if st.button("开始使用"):
                s.add(User(id=str(uuid.uuid4()), username=new_u))
                s.commit()
                st.rerun()
            st.stop()
        
        curr_u_name = st.selectbox("当前操作人", [u.username for u in all_users])
        curr_u = next(u for u in all_users if u.username == curr_u_name)
    
    st.divider()
    tab_nav = st.radio("导航", ["📊 统计中心", "📝 快速记账", "🤝 群组管理"])

# --- Tab 1: 统计与历史 ---
if tab_nav == "📊 统计中心":
    groups = fetch_groups()
    if not groups:
        st.info("去「群组管理」创建一个吧！")
    else:
        for grp in groups:
            with st.container(border=True):
                col_h1, col_h2 = st.columns([3, 1])
                col_h1.subheader(f"📂 {grp.name}")
                
                # 余额与结算
                bals = fetch_balances(grp.id)
                txs = FinanceUtils.simplify(bals)
                
                c1, c2 = st.columns(2)
                with c1:
                    st.caption("💰 结算方案")
                    if not txs: st.write("✅ 账目已平")
                    for t in txs:
                        st.markdown(f"**{t['from']}** ➡ **{t['to']}** : :green[RM {FinanceUtils.to_dollars(t['amount'])}]")
                
                with c2:
                    my_b = bals.get(curr_u.username, 0)
                    st.metric("我的状况", f"RM {FinanceUtils.to_dollars(my_b)}", 
                              delta="需收回" if my_b >=0 else "需支付", delta_color="normal")

                # 历史明细明细
                with st.expander("🕒 历史记录与删除"):
                    with Session() as s:
                        exps = s.query(Expense).filter_by(group_id=grp.id, is_deleted=False).order_by(Expense.date.desc()).all()
                        for e in exps:
                            ec1, ec2, ec3 = st.columns([3, 1, 1])
                            ec1.write(f"{e.date.strftime('%m/%d')} **{e.description}**")
                            ec2.write(f"RM {FinanceUtils.to_dollars(e.amount)}")
                            if ec3.button("🗑️", key=f"del_{e.id}"):
                                s.query(Expense).filter_by(id=e.id).update({"is_deleted": True})
                                s.commit()
                                st.cache_data.clear()
                                st.rerun()

# --- Tab 2: 记账逻辑 (修复核心痛点) ---
elif tab_nav == "📝 快速记账":
    st.header("📝 记录新支出")
    groups = fetch_groups()
    if not groups: st.stop()
    
    g_sel = st.selectbox("选择群组", [g.name for g in groups])
    grp = next(g for g in groups if g.name == g_sel)
    m_names = [m.user.username for m in grp.members]
    m_map = {m.user.username: m.user.id for m in grp.members}
    
    # 基本信息
    row1 = st.columns(3)
    desc = row1[0].text_input("消费内容", "晚餐")
    amt_f = row1[1].number_input("总金额 (RM)", min_value=0.0, step=0.1)
    cat = row1[2].selectbox("分类", ["餐饮", "交通", "购物", "娱乐", "其他"])
    
    total_c = FinanceUtils.to_cents(amt_f)
    
    # 1. 付款方 (支持多人)
    st.divider()
    st.subheader("1. 谁付的钱？")
    p_mode = st.toggle("多人共同垫付", value=False)
    p_splits = {}
    if not p_mode:
        pa = st.selectbox("付款人", m_names, index=m_names.index(curr_u.username) if curr_u.username in m_names else 0)
        p_splits[m_map[pa]] = total_c
    else:
        p_cols = st.columns(len(m_names))
        for i, m in enumerate(m_names):
            v = p_cols[i].number_input(f"{m} 付", min_value=0.0, key=f"p_{m}")
            if v > 0: p_splits[m_map[m]] = FinanceUtils.to_cents(v)

    # 2. 分账模式 (彻底修复跳不出问题)
    st.divider()
    st.subheader("2. 怎么分？")
    s_mode = st.radio("模式", ["均分", "按份数", "百分比", "具体金额"], horizontal=True)
    
    o_splits = {}
    if s_mode == "均分":
        target = st.multiselect("参与人", m_names, default=m_names)
        if target:
            amts = FinanceUtils.distribute(total_c, [1]*len(target))
            for i, m in enumerate(target): o_splits[m_map[m]] = amts[i]
            
    elif s_mode == "按份数":
        s_cols = st.columns(len(m_names))
        weights = []
        for i, m in enumerate(m_names):
            w = s_cols[i].number_input(f"{m}(份)", min_value=0, value=1, key=f"s_{m}")
            weights.append(w)
        amts = FinanceUtils.distribute(total_c, weights)
        for i, m in enumerate(m_names): o_splits[m_map[m]] = amts[i]

    elif s_mode == "具体金额":
        e_cols = st.columns(len(m_names))
        cur_sum = 0
        for i, m in enumerate(m_names):
            v = e_cols[i].number_input(f"{m}(RM)", min_value=0.0, key=f"e_{m}")
            c = FinanceUtils.to_cents(v)
            o_splits[m_map[m]] = c
            cur_sum += c
        if cur_sum != total_c:
            st.warning(f"金额不平：差额 RM {FinanceUtils.to_dollars(total_c - cur_sum)}")

    elif s_mode == "百分比":
        pct_cols = st.columns(len(m_names))
        pcts = []
        for i, m in enumerate(m_names):
            p = pct_cols[i].number_input(f"{m}(%)", min_value=0, max_value=100, key=f"pct_{m}")
            pcts.append(p)
        if sum(pcts) == 100:
            amts = FinanceUtils.distribute(total_c, pcts)
            for i, m in enumerate(m_names): o_splits[m_map[m]] = amts[i]
        else:
            st.error(f"总和必须为100%，当前 {sum(pcts)}%")

    # 提交
    if st.button("🚀 确认记账", type="primary", use_container_width=True):
        if sum(p_splits.values()) != total_c:
            st.error("付款总额不对")
        elif sum(o_splits.values()) != total_c:
            st.error("分摊总额不对")
        else:
            with Session() as s:
                eid = str(uuid.uuid4())
                s.add(Expense(id=eid, group_id=grp.id, created_by=curr_u.id, 
                              description=desc, amount=total_c, category=cat, date=datetime.now()))
                for uid in set(list(p_splits.keys()) + list(o_splits.keys())):
                    s.add(Split(expense_id=eid, user_id=uid, 
                               paid_amount=p_splits.get(uid,0), owed_amount=o_splits.get(uid,0)))
                s.commit()
                st.cache_data.clear()
                st.balloons()
                st.rerun()

# --- Tab 3: 群组管理 (回归功能) ---
elif tab_nav == "🤝 群组管理":
    st.header("🤝 群组与成员")
    
    t1, t2 = st.tabs(["🆕 创建群组", "👥 成员管理"])
    
    with t1:
        g_name = st.text_input("新群组名称")
        with Session() as s:
            users = s.query(User).all()
            selected = st.multiselect("邀请成员", [u.username for u in users], default=[curr_u.username])
            if st.button("创建群组", type="primary"):
                if g_name and selected:
                    gid = str(uuid.uuid4())
                    s.add(Group(id=gid, name=g_name))
                    for name in selected:
                        uid = next(u.id for u in users if u.username == name)
                        s.add(GroupMember(group_id=gid, user_id=uid))
                    s.commit()
                    st.cache_data.clear()
                    st.success(f"群组 {g_name} 创建成功！")
                    st.rerun()

    with t2:
        st.subheader("添加新用户到系统")
        new_user_name = st.text_input("用户名")
        if st.button("添加用户"):
            with Session() as s:
                if not s.query(User).filter_by(username=new_user_name).first():
                    s.add(User(id=str(uuid.uuid4()), username=new_user_name))
                    s.commit()
                    st.success("添加成功")
                    st.rerun()
