/*
 * Feature extraction for training, using the exact code that runs on the board.
 *
 * This file includes board/eog/eog.c, runs the unmodified streaming recognizer over a whole recording
 * with events switched off (so there is no lockout and no blink grouping: every trigger is classified),
 * and copies out the 72 features the recognizer computed for each trigger. Training on these features
 * means the model sees exactly what the C code on the SK-AM62B will compute, sample for sample.
 *
 * Built automatically by dataset.py:  gcc -O2 -shared -fPIC -o build/libeog_features.so eog_features.c -lm
 */
#include "../board/eog/eog.c"
#include <stdlib.h>

int eog_feature_count(void) { return RF_NFEAT; }

/* x: n samples in volts at 500 Hz. Writes up to max triggers: sample index and RF_NFEAT features each.
   Returns the number of triggers written (triggers whose window runs past the end are dropped). */
int eog_extract_features(const float *x, int n, long long *trig, float *feat, int max) {
    eog_t *e = (eog_t *)malloc(sizeof(eog_t));
    if (!e) return -1;
    eog_init(e);
    e->event_thr = 2.0;                       /* 1 - P(none) never reaches 2: no events, no lockout */
    int k = 0;
    int64_t pend_t[64];
    double pend_noise[64];
    for (int i = 0; i < n; i++) {
        int head = e->pend_head;
        memcpy(pend_t, e->pend_t, sizeof(pend_t));
        memcpy(pend_noise, e->pend_noise, sizeof(pend_noise));
        eog_push(e, x[i]);
        int used = (e->pend_head - head + 64) % 64;  /* triggers classified during this sample */
        for (int j = 0; j < used && k < max; j++) {
            int slot = (head + j) % 64;
            trig[k] = (long long)pend_t[slot];
            features(e, pend_t[slot], pend_noise[slot], feat + (size_t)k * RF_NFEAT);
            k++;
        }
    }
    free(e);
    return k;
}
