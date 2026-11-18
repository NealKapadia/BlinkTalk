/* Signal-chain constants for eog.c (filter coefficients from scipy.signal.butter(2, [0.3, 12], 'bandpass', fs=500)) */
#ifndef EOG_CONFIG_H
#define EOG_CONFIG_H
#define EOG_FS 500
static const double EOG_BIQUAD[2][6] = { {0.0048881159337277429, 0.0097762318674554857, 0.0048881159337277429, 1, -1.7969541057552452, 0.81659902038966714}, {1, -2, 1, 1, -1.9946818533596398, 0.99469673199791708} };
#define EOG_MA_LEN 20      /* slope moving average, 40 ms */
#define EOG_K_THR 8.0         /* detector threshold = K x percentile */
#define EOG_PCT 10             /* ...of |slope| over the last 10 s */
#define EOG_HALF 40           /* local-extremum half-window, 80 ms */
#define EOG_WARMUP 1000           /* no detections in the first 2 s */
#define EOG_NOISE_WIN 5000
#define EOG_THR_UPDATE 250
#define EOG_EVENT_THR 0.40     /* event if 1 - P(none) >= this; type = most likely non-none class */
#define EOG_G_MIN 100           /* blink group: next dip 0.2 s ... */
#define EOG_G_MAX 300           /* ... to 0.6 s after the previous one */
#define EOG_G_REL 0.33          /* and at least 1/3 as steep as the first blink */
#define EOG_LOCK 200       /* ignore triggers for 0.4 s after an event */
/* feature windows (samples relative to trigger) */
#define EOG_I_REF0 75
#define EOG_I_REF1 25
#define EOG_Z 150
#define EOG_WLEN 450
#define EOG_POST 175
#define EOG_REV0 20
#define EOG_REV1 200
#define EOG_DUR 150
static const int EOG_LV[5] = {50, 100, 150, 225, 299};
#define EOG_WF_PRE 100      /* waveform: 0.2 s before ... */
#define EOG_WF_POST 400     /* ... 0.8 s after, every 10th sample */
#define EOG_WF_BASE1 50
#define EOG_WF_STEP 10
#define EOG_PRE1 500          /* first-spike search: 1.0 s before the trigger */
#define EOG_LATENCY (EOG_WF_POST - 1) /* decision made this many samples after onset */
#endif
