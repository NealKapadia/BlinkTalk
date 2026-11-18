#include "eog.h"
#include "eog_config.h"
#include "eog_model.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

#define M(i) ((int)((i) & (EOG_RB - 1)))

void eog_init(eog_t *e) {
    memset(e, 0, sizeof(*e));
    e->thr = INFINITY;
    e->lock = -1000000000LL;
    e->event_thr = EOG_EVENT_THR;
}

static int cmpd(const void *p, const void *q) {
    double x = *(const double *)p, y = *(const double *)q;
    return (x > y) - (x < y);
}

/* numpy.percentile(a[max(0,c-W):c], pct), linear interpolation */
static double percentile_before(const eog_t *e, int64_t c, int W, double pct) {
    static double tmp[EOG_NOISE_WIN];
    int64_t lo = c - W < 0 ? 0 : c - W;
    int n = (int)(c - lo);
    if (n <= 0) return INFINITY;
    for (int i = 0; i < n; i++) tmp[i] = e->a[M(lo + i)];
    qsort(tmp, n, sizeof(double), cmpd);
    double pos = (n - 1) * pct / 100.0;
    int i0 = (int)floor(pos);
    int i1 = i0 + 1 < n ? i0 + 1 : i0;
    return tmp[i0] + (tmp[i1] - tmp[i0]) * (pos - i0);
}

/* 17 shape features + 50 normalised waveform samples + log peak = 68 */
static void features(const eog_t *e, int64_t t, double noise, float *out) {
    double f[RF_NFEAT];
    double st = e->s[M(t)];
    double sg = st > 0 ? 1.0 : (st < 0 ? -1.0 : 0.0);
    double a0 = fabs(st);
    double ref = 0;
    for (int64_t i = t - EOG_I_REF0; i < t - EOG_I_REF1; i++) ref += e->y[M(i)];
    ref /= (EOG_I_REF0 - EOG_I_REF1);
    double w[EOG_WLEN], sw[EOG_WLEN];
    for (int i = 0; i < EOG_WLEN; i++) {
        int64_t k = t - EOG_Z + i;
        w[i] = sg * (e->y[M(k)] - ref);
        sw[i] = sg * e->s[M(k)];
    }
    const int z = EOG_Z;
    double pk = -INFINITY; int ipk = 0;
    for (int i = 0; i < EOG_POST; i++) if (w[z + i] > pk) { pk = w[z + i]; ipk = i; }
    pk = (pk > 0 ? pk : 0) + 1e-9;
    double rev = -INFINITY; int irev = 0;
    for (int i = z + EOG_REV0; i < z + EOG_REV1; i++) if (-sw[i] > rev) { rev = -sw[i]; irev = i - (z + EOG_REV0); }
    double pre_same = -INFINITY, pre_opp = -INFINITY;
    for (int i = 0; i < z - EOG_REV0; i++) { if (sw[i] > pre_same) pre_same = sw[i]; if (-sw[i] > pre_opp) pre_opp = -sw[i]; }
    int dur = EOG_DUR;
    for (int i = 0; i < EOG_DUR; i++) if (!(sw[z + i] > 0.5 * a0)) { dur = i; break; }
    double mu = 0, var = 0;
    for (int i = 0; i < z; i++) mu += w[i];
    mu /= z;
    for (int i = 0; i < z; i++) var += (w[i] - mu) * (w[i] - mu);
    double sd = sqrt(var / z);
    int j = 0;
    f[j++] = sg;
    f[j++] = log(a0 / noise);
    f[j++] = log(a0);
    f[j++] = log(pk / noise * EOG_FS);
    f[j++] = log(pk);
    f[j++] = rev / a0;
    f[j++] = (double)irev / EOG_FS;
    f[j++] = pre_same / a0;
    f[j++] = pre_opp / a0;
    f[j++] = (double)dur / EOG_FS;
    f[j++] = (double)ipk / EOG_FS;
    f[j++] = sd / pk;
    for (int q = 0; q < 5; q++) f[j++] = w[z + EOG_LV[q]] / pk;
    /* waveform: y[t-0.2s .. t+0.8s) every 10th sample, minus mean of y[t-0.2s .. t-0.1s) */
    double base = 0;
    for (int64_t i = t - EOG_WF_PRE; i < t - EOG_WF_BASE1; i++) base += e->y[M(i)];
    base /= (EOG_WF_PRE - EOG_WF_BASE1);
    double wf[(EOG_WF_PRE + EOG_WF_POST) / EOG_WF_STEP], mx = 0;
    int nw = 0;
    for (int64_t i = t - EOG_WF_PRE; i < t + EOG_WF_POST; i += EOG_WF_STEP) {
        wf[nw] = e->y[M(i)] - base;
        if (fabs(wf[nw]) > mx) mx = fabs(wf[nw]);
        nw++;
    }
    for (int i = 0; i < nw; i++) f[j++] = wf[i] / mx;
    f[j++] = log(mx);
    /* first spike: a look's opposite-direction movement comes 0.4-0.7 s before the sharp spike, a blink
       has none. Timing-independent: largest excursion / slope either way anywhere in the last 1.0 s. */
    double mainv = 1e-9;
    for (int64_t i = t; i < t + EOG_Z; i++) { double v = fabs(e->y[M(i)] - ref); if (v > mainv) mainv = v; }
    double pmin = INFINITY, pmax = -INFINITY, sopp = -INFINITY, ssame = -INFINITY;
    for (int64_t i = t - EOG_PRE1; i < t - EOG_I_REF1; i++) {
        double v = sg * (e->y[M(i)] - ref), d = sg * e->s[M(i)];
        if (v < pmin) pmin = v;
        if (v > pmax) pmax = v;
        if (-d > sopp) sopp = -d;
        if (d > ssame) ssame = d;
    }
    f[j++] = pmin / mainv;
    f[j++] = pmax / mainv;
    f[j++] = sopp / a0;
    f[j++] = ssame / a0;
    for (int i = 0; i < RF_NFEAT; i++) out[i] = (float)f[i]; /* trees compare in float32, like scikit-learn */
}

static void forest(const float *x, float *p) {
    for (int c = 0; c < RF_NCLASS; c++) p[c] = 0;
    for (int t = 0; t < RF_NTREES; t++) {
        int i = RF_ROOT[t];
        while (RF_FEAT[i] >= 0) i = (x[RF_FEAT[i]] <= RF_THR[i]) ? RF_LEFT[i] : RF_RIGHT[i];
        for (int c = 0; c < RF_NCLASS; c++) p[c] += RF_PROB[i][c];
    }
    for (int c = 0; c < RF_NCLASS; c++) p[c] /= RF_NTREES;
}

static void push_out(eog_t *e, eog_event_t ev, int64_t t) {
    if (e->out_count < 16) {
        int k = (e->out_head + e->out_count) % 16;
        e->out_ev[k] = ev; e->out_t[k] = t; e->out_count++;
    }
}

static void close_group(eog_t *e) {
    switch (e->g_count) {
    case 1: push_out(e, EOG_BLINK, e->g_t0); break;
    case 2: push_out(e, EOG_SELECT, e->g_t0); break;
    case 3: push_out(e, EOG_POWER, e->g_t0); break;
    case 4: break; /* ambiguous: ignored */
    default: push_out(e, EOG_SOS, e->g_t0); break;
    }
    e->g_open = 0;
}

eog_event_t eog_push(eog_t *e, float x) {
    int64_t n = e->n;
    /* 1. bandpass, direct form II transposed (== scipy.signal.sosfilt) */
    double v = x;
    for (int k = 0; k < 2; k++) {
        const double *c = EOG_BIQUAD[k];
        double out = c[0] * v + e->z[k][0];
        e->z[k][0] = c[1] * v - c[4] * out + e->z[k][1];
        e->z[k][1] = c[2] * v - c[5] * out;
        v = out;
    }
    double y = v;
    /* 2. slope = causal 40 ms moving average of dy/dt */
    double d = (n == 0) ? 0.0 : (y - e->y_prev) * EOG_FS;
    e->y_prev = y;
    e->d_hist[n % EOG_MA_LEN] = d;
    double sum = 0;
    int cnt = n + 1 < EOG_MA_LEN ? (int)(n + 1) : EOG_MA_LEN;
    for (int i = 0; i < cnt; i++) sum += e->d_hist[i];
    double s = sum / EOG_MA_LEN;
    e->y[M(n)] = y; e->s[M(n)] = s; e->a[M(n)] = fabs(s);
    e->n = n + 1;

    /* 3. detector on candidate c (needs EOG_HALF samples of look-ahead) */
    int64_t c = n - EOG_HALF;
    if (c >= EOG_WARMUP) {
        if (c % EOG_THR_UPDATE == 0)
            e->thr = EOG_K_THR * percentile_before(e, c, EOG_NOISE_WIN, EOG_PCT);
        double sc = e->s[M(c)];
        if (fabs(sc) > e->thr && sc != 0) {
            int ext = 1;
            for (int64_t i = c - EOG_HALF; i <= c + EOG_HALF && ext; i++)
                if ((sc > 0 && e->s[M(i)] > sc) || (sc < 0 && e->s[M(i)] < sc)) ext = 0;
            if (ext && e->pend_count < 64) {
                int slot = (e->pend_head + e->pend_count) % 64;
                e->pend_t[slot] = c;
                e->pend_noise[slot] = percentile_before(e, c, EOG_NOISE_WIN, EOG_PCT) + 1e-12;
                e->pend_count++;
            }
        }
    }
    /* 4. classify triggers whose window is complete, in time order */
    while (e->pend_count > 0) {
        int64_t t = e->pend_t[e->pend_head];
        if (n < t + EOG_LATENCY) break;
        double noise = e->pend_noise[e->pend_head];
        e->pend_head = (e->pend_head + 1) % 64; e->pend_count--;
        double st = e->s[M(t)], at = fabs(st);
        if (e->g_open) {
            if (st < 0 && t - e->g_last >= EOG_G_MIN && t - e->g_last <= EOG_G_MAX && at >= EOG_G_REL * e->g_a0) {
                e->g_count++; e->g_last = t; e->lock = t + EOG_LOCK; continue;
            }
            if (t - e->g_last > EOG_G_MAX) close_group(e);
        }
        if (t < e->lock) continue;
        float f[RF_NFEAT], p[RF_NCLASS];
        features(e, t, noise, f);
        forest(f, p);
        memcpy(e->last_prob, p, sizeof(p));
        if (1.0 - p[0] >= e->event_thr) {
            int k = 1;
            for (int q = 2; q < RF_NCLASS; q++) if (p[q] > p[k]) k = q;
            if (k == 1) {
                if (e->g_open) close_group(e);
                e->g_open = 1; e->g_count = 1; e->g_t0 = t; e->g_last = t; e->g_a0 = at;
            } else {
                if (e->swap_lr) k = (k == EOG_LEFT) ? EOG_RIGHT : EOG_LEFT;
                push_out(e, (eog_event_t)k, t);
            }
            e->lock = t + EOG_LOCK;
        }
    }
    /* 5. close a blink group once no further dip can arrive */
    if (e->g_open && n - EOG_LATENCY > e->g_last + EOG_G_MAX) close_group(e);
    /* 6. hand out one queued event */
    eog_event_t ev = EOG_NONE;
    if (e->out_count > 0) {
        ev = e->out_ev[e->out_head]; e->last_event_onset = e->out_t[e->out_head];
        e->out_head = (e->out_head + 1) % 16; e->out_count--;
    }
    return ev;
}
