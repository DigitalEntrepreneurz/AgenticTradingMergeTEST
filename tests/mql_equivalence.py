"""Independent re-implementation of the EMITTED MQL expressions.

This does NOT call _rule_vote. It parses what the compiler actually emitted and
evaluates those semantics against the same indicator series MQL would read,
so a mistranslation in firm/mql.py cannot hide behind shared code.
"""
import math
from firm.brokers.simulated import SimulatedBroker
from firm.strategies.composite import evaluate_spec
from firm.indicators import closes, sma, ema, rsi, atr, macd, bollinger, donchian
from firm.scout import CATALOGUE

br=SimulatedBroker({"id":"s"}); br.connect()
bars=br.bars("EURUSD","H1",int(__import__("os").environ.get("EQUIV_BARS","1500")))
c=closes(bars)
ATR=atr(bars,14)

def EMA(n,s): return ema(c,n)[len(c)-1-0] if False else None

def mql_rule(rule, i):
    """Evaluate the rule the way the generated MQL does (shift 1 == index i)."""
    t=rule.get("type")
    def E(n,sh=1): 
        v=ema(c,n); j=i-(sh-1)
        return v[j] if 0<=j<len(v) and v[j] is not None else 0.0
    def S(n,sh=1):
        v=sma(c,n); j=i-(sh-1)
        return v[j] if 0<=j<len(v) and v[j] is not None else 0.0
    def R(n,sh=1):
        v=rsi(c,n); j=i-(sh-1)
        return v[j] if 0<=j<len(v) and v[j] is not None else 0.0
    def M(f,s_,g,buf,sh=1):
        line,sig,_=macd(c,f,s_,g); j=i-(sh-1)
        arr=line if buf==0 else sig
        return arr[j] if 0<=j<len(arr) and arr[j] is not None else 0.0
    def B(n,m,which,sh=1):
        up,mid,lo=bollinger(c,n,m); j=i-(sh-1)
        arr=up if which=="upper" else lo
        return arr[j] if 0<=j<len(arr) and arr[j] is not None else 0.0
    def Cl(sh): return bars[i-(sh-1)].close
    def Op(sh): return bars[i-(sh-1)].open
    def Hi(sh): return bars[i-(sh-1)].high
    def Lo(sh): return bars[i-(sh-1)].low
    def HH(n,shift):
        seg=[b.high for b in bars[i-(shift-1)-n+1:i-(shift-1)+1]]
        return max(seg) if len(seg)==n else 0.0
    def LL(n,shift):
        seg=[b.low for b in bars[i-(shift-1)-n+1:i-(shift-1)+1]]
        return min(seg) if len(seg)==n else 0.0
    a=ATR[i] if ATR[i] else 0.0

    if t=="ema_stack":
        f,m,s_=E(int(rule.get("fast",9))),E(int(rule.get("mid",21))),E(int(rule.get("slow",50)))
        if f<=0 or m<=0 or s_<=0: return 0
        if f>m and m>s_: return 1
        if f<m and m<s_: return -1
        return 0
    if t in ("ema_cross","sma_cross"):
        g=E if t=="ema_cross" else S
        fn,sn=int(rule.get("fast",9)),int(rule.get("slow",21))
        f0,s0,f1,s1=g(fn,1),g(sn,1),g(fn,2),g(sn,2)
        if min(f0,s0,f1,s1)<=0: return 0
        if f1<=s1 and f0>s0: return 1
        if f1>=s1 and f0<s0: return -1
        return 0
    if t=="rsi_zone":
        r=R(int(rule.get("period",14)))
        lo,hi=float(rule.get("min",40)),float(rule.get("max",70))
        if r<=0: return 0
        if lo<=r<=hi: return 1 if r>=50 else -1
        return 0
    if t=="rsi_extreme":
        r=R(int(rule.get("period",14)))
        if r<=0: return 0
        if r<float(rule.get("oversold",30)): return 1
        if r>float(rule.get("overbought",70)): return -1
        return 0
    if t=="macd_cross":
        f,s_,g=int(rule.get("fast",12)),int(rule.get("slow",26)),int(rule.get("signal",9))
        m0,g0,m1,g1=M(f,s_,g,0,1),M(f,s_,g,1,1),M(f,s_,g,0,2),M(f,s_,g,1,2)
        if m1<=g1 and m0>g0: return 1
        if m1>=g1 and m0<g0: return -1
        return 0
    if t=="bb_touch":
        n,m=int(rule.get("period",20)),float(rule.get("mult",2.0))
        up,lo=B(n,m,"upper"),B(n,m,"lower"); px=Cl(1)
        rev=rule.get("mode","reversion")=="reversion"
        below,above=(1,-1) if rev else (-1,1)
        if up<=0 or lo<=0: return 0
        if px<lo: return below
        if px>up: return above
        return 0
    if t=="breakout":
        n=int(rule.get("period",20))
        hh,ll,px=HH(n,2),LL(n,2),Cl(1)
        if hh<=0 or ll<=0: return 0
        if px>hh: return 1
        if px<ll: return -1
        return 0
    if t=="pullback":
        ref=rule.get("to","ema_mid")
        n={"ema_fast":int(rule.get("fast",9)),"ema_mid":int(rule.get("mid",21)),
           "ema_slow":int(rule.get("slow",50))}.get(ref,int(rule.get("to_period",21)))
        ma=E(n); px=Cl(1)
        if ma<=0 or a<=0: return 0
        return 1 if abs(px-ma)/a<=float(rule.get("max_atr",1.0)) else 0
    if t=="candle":
        o1,h1,l1,c1=Op(1),Hi(1),Lo(1),Cl(1); o2,c2=Op(2),Cl(2)
        body=abs(c1-o1); rng=max(h1-l1,1e-12); pbody=abs(c2-o2)
        bull=c1>o1 and c2<o2 and body>pbody
        bear=c1<o1 and c2>o2 and body>pbody
        lw=min(o1,c1)-l1; uw=h1-max(o1,c1)
        pb=lw>body*2 and body/rng<0.4
        pr=uw>body*2 and body/rng<0.4
        if bull or pb: return 1
        if bear or pr: return -1
        return 0
    if t=="session":
        import time as _t
        hr=int(_t.strftime("%H",_t.gmtime(bars[i].time)))
        return 0 if (int(rule.get("from",0))<=hr<int(rule.get("to",24))) else -99
    if t=="atr_filter":
        px=Cl(1)
        pct = a/px*100 if (px>0 and a>0) else -1
        if pct<0: return 0
        return 0 if float(rule.get("min_pct",0))<=pct<=float(rule.get("max_pct",99)) else -99
    return 0

def mql_decide(spec,i,agreement):
    for f in spec.get("filters",[]):
        if mql_rule(f,i)==-99: return 0
    votes=[]
    for r in spec.get("entry",[]):
        v=mql_rule(r,i)
        if v==-99: return 0
        votes.append(v)
    if not votes: return 0
    bulls=sum(1 for v in votes if v>0); bears=sum(1 for v in votes if v<0)
    tot=len(votes)
    if bulls/tot>=agreement and bulls>bears: return 1
    if bears/tot>=agreement and bears>bulls: return -1
    return 0

total=mismatch=sigs=0
for spec in CATALOGUE:
    py=evaluate_spec(spec,bars)
    agr=float(spec.get("agreement",0.6))
    bad=[]
    for i in range(160,len(bars)):
        want = 0 if py[i] is None else (1 if py[i].side=="buy" else -1)
        a=ATR[i]
        got = mql_decide(spec,i,agr) if (a and a>0) else 0
        if want!=got: bad.append(i)
        total+=1
        if want: sigs+=1
    mismatch+=len(bad)
    flag="OK " if not bad else "MISMATCH"
    print(f"{flag} {spec['name'][:36]:36} sigs={sum(1 for s in py if s):4} bad={len(bad)}")
print(f"\n{total} decisions, {sigs} signals, {mismatch} mismatches")
import sys
sys.exit(1 if mismatch else 0)
