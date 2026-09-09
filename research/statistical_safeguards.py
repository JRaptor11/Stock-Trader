"""Small dependency-free safeguards for broad strategy tournaments."""
import math, random, statistics

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


def paired_block_bootstrap(candidate: list[float], benchmark: list[float], *,
                           block_size: int = 5, samples: int = 2_000,
                           seed: int = 7) -> dict:
    """Deterministic moving-block bootstrap for paired session returns."""
    if len(candidate) != len(benchmark):
        raise ValueError("candidate and benchmark returns must be paired")
    if block_size < 1 or samples < 1:
        raise ValueError("block_size and samples must be positive")
    excess=[float(left)-float(right) for left,right in zip(candidate,benchmark)]
    if not excess:
        return {"observations":0,"block_size":block_size,"samples":samples,
                "mean_excess_return":None,"excess_return_ci_95":None,
                "probability_mean_excess_positive":None}
    rng=random.Random(seed); n=len(excess); starts=range(max(1,n-block_size+1)); estimates=[]
    for _ in range(samples):
        draw=[]
        while len(draw)<n:
            start=rng.choice(starts); draw.extend(excess[start:min(n,start+block_size)])
        estimates.append(statistics.fmean(draw[:n]))
    estimates.sort()
    def quantile(probability):
        position=(len(estimates)-1)*probability; low=math.floor(position); high=math.ceil(position)
        return estimates[low] if low==high else estimates[low]*(high-position)+estimates[high]*(position-low)
    return {"observations":n,"block_size":block_size,"samples":samples,
            "mean_excess_return":statistics.fmean(excess),
            "excess_return_ci_95":[quantile(.025),quantile(.975)],
            "probability_mean_excess_positive":sum(value>0 for value in estimates)/len(estimates)}
