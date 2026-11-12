#!/usr/bin/env python3
import os
import sys
import json
import math
import random
from datetime import datetime, timezone

def parse_iso(s):
    if isinstance(s, datetime):
        return s
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)

def main():
    input_dir = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
    output_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(output_dir, exist_ok=True)

    task_path = os.path.join(input_dir, "task.json")
    history_path = os.path.join(input_dir, "history.ndjson")

    if not os.path.exists(task_path) or not os.path.exists(history_path):
        input_dir = "../kit/input"
        task_path = os.path.join(input_dir, "task.json")
        history_path = os.path.join(input_dir, "history.ndjson")

    with open(task_path, "r") as f:
        task = json.load(f)

    history_until = parse_iso(task["history_until"])
    reads = [parse_iso(r) for r in task["reads"]]
    edges = task["size"]["edges"] # 81 edges

    marks = []
    with open(history_path, "r") as f:
        for line in f:
            if line.strip():
                marks.append(json.loads(line))

    if len(marks) <= 3:
        last_rows = marks[-1].get("rows", [])[:5] if marks else []
        last_mints = [r["mint"] for r in last_rows]
        probabilities = []
        for idx, mint in enumerate(last_mints):
            place = idx + 1
            probabilities.append({"mint": mint, "place": place, "p": 0.30})
            if place > 1:
                probabilities.append({"mint": last_mints[place - 2], "place": place, "p": 0.10})
            if place < 5:
                probabilities.append({"mint": last_mints[place], "place": place, "p": 0.10})
        top5 = last_mints + [""] * (5 - len(last_mints))
        sizes = []
        for idx, r in enumerate(last_rows):
            place = idx + 1
            cap_lamports = int(r["market_cap_lamports"])
            log_cap = math.log10(cap_lamports) - 9.0
            bin_idx = 81 if log_cap >= 6.0 else (0 if log_cap < 2.0 else next((i for i, e in enumerate(edges) if log_cap < e), 81))
            sizes.append({"place": place, "bin": bin_idx, "p": 0.70})
        forecast = {"probabilities": probabilities, "top5": top5, "sizes": sizes}
        output_path = os.path.join(output_dir, "forecast.json")
        with open(output_path, "w") as f:
            json.dump(forecast, f, indent=2)
        return

    # Parse coin histories
    coin_data = {} # mint -> { 'symbol': ..., 'created_at': ..., 'caps': [(time_sec, log_cap, venue)] }
    total_marks = len(marks)

    for m in marks:
        at_time = parse_iso(m["at"])
        for r in m.get("rows", []):
            mint = r["mint"]
            cap_lamports = int(r["market_cap_lamports"])
            if cap_lamports <= 0:
                continue
            log_cap = math.log10(cap_lamports) - 9.0 # in SOL
            created_at = parse_iso(r["created_at"])

            if mint not in coin_data:
                coin_data[mint] = {
                    "symbol": r.get("symbol", mint[:6]),
                    "created_at": created_at,
                    "history": []
                }
            coin_data[mint]["history"].append((at_time, log_cap, r.get("venue", "curve")))

    history_until_sec = history_until.timestamp()

    # Compute features at cut
    coin_features = {}
    young_caps = [] # caps of coins observed in their first 3 hours

    for mint, data in coin_data.items():
        hist = data["history"]
        if not hist:
            continue
        past_hist = [h for h in hist if h[0].timestamp() <= history_until_sec]
        if not past_hist:
            continue

        last_entry = past_hist[-1]
        last_cap = last_entry[1]
        created_sec = data["created_at"].timestamp()
        age_hours = max(0.1, (history_until_sec - created_sec) / 3600.0)

        if age_hours <= 3.0:
            young_caps.append(last_cap)

        peak_cap = max(h[1] for h in past_hist)
        recent = past_hist[-max(1, len(past_hist)//3):]
        if len(recent) >= 2:
            dt = (recent[-1][0].timestamp() - recent[0][0].timestamp()) / 3600.0
            slope = (recent[-1][1] - recent[0][1]) / dt if dt > 0 else 0.0
        else:
            slope = 0.0

        if len(past_hist) >= 3:
            changes = [past_hist[i][1] - past_hist[i-1][1] for i in range(1, len(past_hist))]
            mean_c = sum(changes) / len(changes)
            vol = math.sqrt(sum((c - mean_c)**2 for c in changes) / len(changes))
        else:
            vol = 0.15

        graduated = any(h[2] == "pool" for h in past_hist)
        presence_share = len(past_hist) / total_marks

        # Generous combined score for widening candidates to ~250
        score = last_cap + 0.2 * slope + 0.5 * peak_cap + 1.0 * presence_share

        coin_features[mint] = {
            "symbol": data["symbol"],
            "last_cap": last_cap,
            "age_hours": age_hours,
            "peak_cap": peak_cap,
            "slope": slope,
            "vol": max(0.05, vol),
            "graduated": graduated,
            "score": score
        }

    if not young_caps:
        young_caps = [2.0, 2.5, 3.0]

    # Select candidates (top ~250) sorted stably by score then mint
    sorted_coins = sorted(coin_features.items(), key=lambda x: (x[1]["score"], x[0]), reverse=True)
    candidates = [mint for mint, feat in sorted_coins[:250]]

    # Estimate horizon-aware newcomer share per place from pseudo-cuts in history
    pseudo_newcomer_obs = {k: [] for k in range(1, 6)}
    if len(marks) >= 4:
        for cut_idx in range(2, len(marks) - 1):
            cut_time = parse_iso(marks[cut_idx]["at"])
            for fut_idx in range(cut_idx + 1, len(marks)):
                fut_mark = marks[fut_idx]
                h = (parse_iso(fut_mark["at"]).timestamp() - cut_time.timestamp()) / 3600.0
                top5 = fut_mark.get("rows", [])[:5]
                for p_idx, r in enumerate(top5):
                    place = p_idx + 1
                    created_at = parse_iso(r["created_at"])
                    is_new = 1.0 if created_at.timestamp() > cut_time.timestamp() else 0.0
                    pseudo_newcomer_obs[place].append((h, is_new))

    read_horizons = [(parse_iso(r).timestamp() - history_until_sec) / 3600.0 for r in reads]

    newcomer_shares = {}
    for place in range(1, 6):
        obs = pseudo_newcomer_obs[place]
        place_nu_estimates = []
        for h_target in read_horizons:
            if obs:
                weights = []
                vals = []
                for h_obs, is_new in obs:
                    w = math.exp(-abs(h_obs - h_target) / 2.0)
                    weights.append(w)
                    vals.append(is_new)
                total_w = sum(weights)
                if total_w > 1e-6:
                    est = sum(w * v for w, v in zip(weights, vals)) / total_w
                else:
                    est = 0.45
            else:
                est = 0.45
            place_nu_estimates.append(est)

        avg_nu = sum(place_nu_estimates) / len(place_nu_estimates) if place_nu_estimates else 0.45
        n_obs = len(obs)
        shrink_weight = n_obs / (n_obs + 10.0)
        nu_shrunk = shrink_weight * avg_nu + (1.0 - shrink_weight) * 0.45
        newcomer_shares[place] = max(0.15, min(0.65, nu_shrunk))

    # Monte Carlo over reads
    rng = random.Random(20261112)
    n_draws = 4000

    read_place_counts = [ {k: {} for k in range(1, 6)} for _ in reads ]
    read_place_sizes = [ {k: {} for k in range(1, 6)} for _ in reads ]
    mc_newcomer_counts = {k: 0 for k in range(1, 6)}

    for r_idx, read_time in enumerate(reads):
        h_hours = max(0.1, (read_time.timestamp() - history_until_sec) / 3600.0)

        for draw in range(n_draws):
            draw_caps = {}
            for mint in candidates:
                feat = coin_features[mint]
                drift = 0.1 * feat["slope"]
                mu = feat["last_cap"] + drift * h_hours
                sigma = feat["vol"] * math.sqrt(h_hours) + 0.05
                sampled_cap = rng.gauss(mu, sigma)
                draw_caps[mint] = sampled_cap

            newcomer_lambda = 8.0 * h_hours
            n_newcomers = max(0, min(50, int(rng.gauss(newcomer_lambda, math.sqrt(newcomer_lambda)))))

            for i in range(n_newcomers):
                base_c = rng.choice(young_caps)
                nc_cap = base_c + rng.gauss(0.2, 0.4) * h_hours
                nc_mint = f"newcomer_{r_idx}_{draw}_{i}"
                draw_caps[nc_mint] = nc_cap

            ranked_draw = sorted(draw_caps.items(), key=lambda x: (x[1], x[0]), reverse=True)
            top_draw = ranked_draw[:5]

            for place_idx, (mint, cap) in enumerate(top_draw):
                place = place_idx + 1
                if mint.startswith("newcomer_"):
                    mc_newcomer_counts[place] += 1
                else:
                    read_place_counts[r_idx][place][mint] = read_place_counts[r_idx][place].get(mint, 0) + 1

                # size bin with horizon dispersion growth
                cap_for_size = cap + rng.gauss(0.0, 0.03 * math.sqrt(h_hours))
                if cap_for_size >= 6.0:
                    bin_idx = 81
                elif cap_for_size < 2.0:
                    bin_idx = 0
                else:
                    for b_i, edge in enumerate(edges):
                        if cap_for_size < edge:
                            bin_idx = b_i
                            break
                    else:
                        bin_idx = 81

                read_place_sizes[r_idx][place][bin_idx] = read_place_sizes[r_idx][place].get(bin_idx, 0) + 1

        # Add score-based pseudo-counts for top candidates across read draws stably (broader coverage, naming 40-80 coins)
        for place in range(1, 6):
            for idx, mint in enumerate(candidates[:150]):
                if mint not in read_place_counts[r_idx][place]:
                    pseudo_c = math.exp(-idx / 35.0) * 8.0
                    read_place_counts[r_idx][place][mint] = read_place_counts[r_idx][place].get(mint, 0.0) + pseudo_c

    # Blend newcomer shares 50/50 with MC newcomer frequency
    total_mc_place_draws = len(reads) * n_draws
    effective_newcomer_shares = {}
    for place in range(1, 6):
        mc_nu = mc_newcomer_counts[place] / total_mc_place_draws
        est_nu = newcomer_shares[place]
        blended_nu = 0.5 * est_nu + 0.5 * mc_nu
        effective_newcomer_shares[place] = max(0.15, min(0.65, blended_nu))

    # Determine global top5 across reads (stably sorted by score, mint)
    aggregate_mint_scores = {}
    for r_idx in range(len(reads)):
        for place in range(1, 6):
            for mint, count in read_place_counts[r_idx][place].items():
                prob = count / n_draws
                aggregate_mint_scores[mint] = aggregate_mint_scores.get(mint, 0.0) + prob * (6 - place)

    top5_mints = [m for m, _ in sorted(aggregate_mint_scores.items(), key=lambda item: (item[1], item[0]), reverse=True)][:5]
    while len(top5_mints) < 5 and candidates:
        for c in candidates:
            if c not in top5_mints:
                top5_mints.append(c)
                break
    top5_mints = top5_mints[:5]

    # Build probabilities rows
    probabilities_rows = []
    last_mark_rows = marks[-1].get("rows", [])[:5] if marks else []
    last_mark_mints = [r["mint"] for r in last_mark_rows]

    for place in range(1, 6):
        nu = effective_newcomer_shares[place]
        place_agg = {}
        for r_idx in range(len(reads)):
            for mint, count in read_place_counts[r_idx][place].items():
                place_agg[mint] = place_agg.get(mint, 0) + count

        total_place_draws = len(reads) * n_draws

        pers_dict = {}
        if place - 1 < len(last_mark_mints):
            pers_dict[last_mark_mints[place - 1]] = 0.30
        if place > 1 and place - 2 < len(last_mark_mints):
            pers_dict[last_mark_mints[place - 2]] = pers_dict.get(last_mark_mints[place - 2], 0.0) + 0.10
        if place < 5 and place < len(last_mark_mints):
            pers_dict[last_mark_mints[place]] = pers_dict.get(last_mark_mints[place], 0.0) + 0.10

        combined_mints = sorted(list(set(place_agg.keys()).union(pers_dict.keys())))
        place_rows_map = {}
        is_short_history = len(marks) <= 4

        for mint in combined_mints:
            mc_freq = place_agg.get(mint, 0) / total_place_draws
            mc_p = mc_freq * (1.0 - nu)
            pers_p = pers_dict.get(mint, 0.0) * (1.0 - nu)

            if is_short_history:
                p_val = pers_p if pers_p > 0 else mc_p
            else:
                p_val = 0.4 * mc_p + 0.6 * pers_p
            if p_val >= 0.0001:
                place_rows_map[mint] = p_val

        # Up to 100 rows per place, stably sorted by probability descending, then mint ascending
        sorted_place_coins = sorted(place_rows_map.items(), key=lambda x: (x[1], x[0]), reverse=True)[:100]
        for mint, p_val in sorted_place_coins:
            probabilities_rows.append({
                "mint": mint,
                "place": place,
                "p": round(p_val, 6)
            })

    # Build sizes (stably sorted)
    sizes_rows = []
    for place in range(1, 6):
        bin_counts = {}
        for r_idx in range(len(reads)):
            for b_idx, count in read_place_sizes[r_idx][place].items():
                bin_counts[b_idx] = bin_counts.get(b_idx, 0) + count

        total_b_draws = len(reads) * n_draws
        size_mass = 0.95 # leave 5% unnamed size mass
        sorted_bins = sorted(bin_counts.items(), key=lambda x: x[0])
        for b_idx, count in sorted_bins:
            p_bin = (count / total_b_draws) * size_mass
            if p_bin >= 0.0001:
                sizes_rows.append({
                    "place": place,
                    "bin": int(b_idx),
                    "p": round(p_bin, 6)
                })

    forecast = {
        "probabilities": probabilities_rows,
        "top5": top5_mints,
        "sizes": sizes_rows
    }

    output_path = os.path.join(output_dir, "forecast.json")
    with open(output_path, "w") as f:
        json.dump(forecast, f, indent=2)

    print(f"Successfully generated forecast at {output_path}")

if __name__ == "__main__":
    main()
