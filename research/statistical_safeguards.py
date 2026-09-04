"""Small dependency-free safeguards for broad strategy tournaments."""
import math, statistics

def return_evidence(values: list[float], family_trials: int) -> dict:
    n=len(values); mean=statistics.fmean(values) if values else None
    if n>1 and statistics.stdev(values)>0:
        statistic=mean/(statistics.stdev(values)/math.sqrt(n)); p=min(1.,math.erfc(abs(statistic)/math.sqrt(2)))
    else: p=None
    ordered=sorted(values,reverse=True); remove_1=max(1,math.ceil(n*.01)) if n else 0; remove_5=max(1,math.ceil(n*.05)) if n else 0
    compound=lambda rows: math.prod(1+v for v in rows)-1 if rows else None
    return {"observations":n,"mean_return":mean,"approximate_two_sided_p":p,"bonferroni_family_trials":family_trials,
            "bonferroni_adjusted_p":min(1.,p*max(1,family_trials)) if p is not None else None,
            "return_without_best_trade":compound(ordered[1:]),"return_without_best_1pct":compound(ordered[remove_1:]),
            "return_without_best_5pct":compound(ordered[remove_5:])}
