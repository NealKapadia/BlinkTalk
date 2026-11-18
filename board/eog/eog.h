/*
 * eog.h - streaming single-channel EOG command recognizer
 *         events: BLINK (1 blink), SELECT (2 quick blinks), POWER (3), SOS (5+), LEFT, RIGHT
 *         (a group of exactly 4 blinks is ignored so a near-miss SOS never toggles power)
 *
 * Pipeline (all causal, 500 Hz input):
 *   bandpass 0.3-12 Hz (2 biquads) -> slope (40 ms moving average of dy/dt)
 *   -> trigger detector: positive and negative slope peaks found separately (+/-80 ms),
 *      above 8 x the 10th percentile of |slope| over the last 10 s
 *   -> 72 features: 17 shape features, 4 first-spike features (a look starts with a small opposite
 *      movement 0.4-0.7 s earlier, a blink does not), 50 normalised waveform samples, log amplitude
 *   -> random forest (50 trees, depth 8): none / blink / left / right
 *   -> event if 1 - P(none) >= 0.40 (type = most likely of blink/left/right), 0.4 s lockout
 *   -> blink grouping: further dips 0.2-0.6 s after the previous one, at least 1/3 as steep,
 *      are counted into the same group: 1 = BLINK, 2 = SELECT, 3 = POWER, 4 = ignored, 5+ = SOS
 *
 * Latency: LEFT/RIGHT 0.8 s after onset; blink events ~1.4 s after the last blink of the group.
 * Memory : ~200 KB state per instance + ~120 KB constant tables (eog_model.h), no malloc.
 */
#ifndef EOG_H
#define EOG_H
#include <stdint.h>

typedef enum { EOG_NONE = 0, EOG_BLINK = 1, EOG_LEFT = 2, EOG_RIGHT = 3, EOG_SELECT = 4, EOG_POWER = 5, EOG_SOS = 6 } eog_event_t;

#define EOG_RB 8192 /* ring buffer length, power of two */

typedef struct {
    double z[2][2];
    double y_prev;
    double d_hist[32];
    double y[EOG_RB], s[EOG_RB], a[EOG_RB];
    int64_t n;
    double thr;
    int64_t pend_t[64];       /* triggers waiting for their feature window */
    double pend_noise[64];
    int pend_head, pend_count;
    int64_t lock;
    /* open blink group */
    int g_open, g_count;
    int64_t g_t0, g_last;
    double g_a0;
    /* output queue */
    eog_event_t out_ev[16];
    int64_t out_t[16];
    int out_head, out_count;
    double event_thr;        /* 1 - P(none) needed for an event (default EOG_EVENT_THR) */
    int swap_lr;             /* 1 = electrodes mirrored: report left as right and vice versa */
    float last_prob[4];
    int64_t last_event_onset;
} eog_t;

void eog_init(eog_t *e);

/* Push one raw sample (volts, 500 Hz). Returns an event when one is decided, else EOG_NONE.
   The onset sample index of the returned event is in e->last_event_onset. */
eog_event_t eog_push(eog_t *e, float x);

#endif
