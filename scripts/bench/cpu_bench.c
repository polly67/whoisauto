/*
 * CPU microbenchmarks for the macOS runner.
 *
 * Single-thread integer latency/throughput, FP32 FMA throughput, streaming
 * memory bandwidth, then the same integer/FMA work spread across every
 * logical core. Emits one JSON object on stdout; run_bench.py folds it into
 * the report.
 */
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>
#include <time.h>

#if defined(__AVX2__) && defined(__FMA__)
#include <immintrin.h>
#define ISA_NAME "avx2"
#else
#define ISA_NAME "scalar"
#endif

#define INT_CHAIN_ITERS 200000000ULL
#define INT_TP_ITERS    200000000ULL
#define FMA_ITERS       100000000ULL
#define MT_ITERS        (INT_TP_ITERS / 2)
#define MT_FMA_ITERS    (FMA_ITERS / 2)
#define MEM_BYTES       (512ULL << 20)
#define REPS            3

static volatile uint64_t sink_u64;
static volatile float sink_f32;

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static uint64_t sysctl_u64(const char *name)
{
    uint64_t v = 0;
    size_t len = sizeof(v);
    return sysctlbyname(name, &v, &len, NULL, 0) == 0 ? v : 0;
}

static void sysctl_str(const char *name, char *buf, size_t len)
{
    size_t n = len;
    if (sysctlbyname(name, buf, &n, NULL, 0) != 0 || n == 0)
        buf[0] = '\0';
}

static void json_str(const char *s)
{
    putchar('"');
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') {
            putchar('\\');
            putchar(c);
        } else if (c < 0x20) {
            printf("\\u%04x", c);
        } else {
            putchar(c);
        }
    }
    putchar('"');
}

/* Dependent xorshift64 chain: latency of one 3-op round trip per iteration. */
static double int_chain_once(uint64_t iters)
{
    uint64_t x = 0x9E3779B97F4A7C15ULL;
    double t0 = now_s();
    for (uint64_t i = 0; i < iters; i++) {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
    }
    double dt = now_s() - t0;
    sink_u64 = x;
    return dt;
}

/* Eight independent LCG streams: throughput, not latency bound. */
static void int_loop(uint64_t iters)
{
    uint64_t a0 = 0x243F6A8885A308D3ULL, a1 = 0x13198A2E03707344ULL;
    uint64_t a2 = 0xA4093822299F31D0ULL, a3 = 0x082EFA98EC4E6C89ULL;
    uint64_t a4 = 0x452821E638D01377ULL, a5 = 0xBE5466CF34E90C6CULL;
    uint64_t a6 = 0xC0AC29B7C97C50DDULL, a7 = 0x3F84D5B5B5470917ULL;
    const uint64_t mul = 6364136223846793005ULL;
    const uint64_t inc = 1442695040888963407ULL;

    for (uint64_t i = 0; i < iters; i++) {
        a0 = a0 * mul + inc;
        a1 = a1 * mul + inc;
        a2 = a2 * mul + inc;
        a3 = a3 * mul + inc;
        a4 = a4 * mul + inc;
        a5 = a5 * mul + inc;
        a6 = a6 * mul + inc;
        a7 = a7 * mul + inc;
    }
    sink_u64 = a0 ^ a1 ^ a2 ^ a3 ^ a4 ^ a5 ^ a6 ^ a7;
}

static double int_tp_once(uint64_t iters, double *gops)
{
    double t0 = now_s();
    int_loop(iters);
    double dt = now_s() - t0;
    *gops = 8.0 * (double)iters / dt / 1e9;
    return dt;
}

#if defined(__AVX2__) && defined(__FMA__)
/* Eight independent 256-bit FMA chains: 8 lanes * 8 chains * 2 flops. */
#define FLOPS_PER_ITER 128.0

static void fma_loop(uint64_t iters)
{
    __m256 a0 = _mm256_set1_ps(1.0f), a1 = _mm256_set1_ps(2.0f);
    __m256 a2 = _mm256_set1_ps(3.0f), a3 = _mm256_set1_ps(4.0f);
    __m256 a4 = _mm256_set1_ps(5.0f), a5 = _mm256_set1_ps(6.0f);
    __m256 a6 = _mm256_set1_ps(7.0f), a7 = _mm256_set1_ps(8.0f);
    const __m256 b = _mm256_set1_ps(1.0000001f);
    const __m256 c = _mm256_set1_ps(1e-7f);

    for (uint64_t i = 0; i < iters; i++) {
        a0 = _mm256_fmadd_ps(a0, b, c);
        a1 = _mm256_fmadd_ps(a1, b, c);
        a2 = _mm256_fmadd_ps(a2, b, c);
        a3 = _mm256_fmadd_ps(a3, b, c);
        a4 = _mm256_fmadd_ps(a4, b, c);
        a5 = _mm256_fmadd_ps(a5, b, c);
        a6 = _mm256_fmadd_ps(a6, b, c);
        a7 = _mm256_fmadd_ps(a7, b, c);
    }

    __m256 s = _mm256_add_ps(_mm256_add_ps(a0, a1), _mm256_add_ps(a2, a3));
    s = _mm256_add_ps(s, _mm256_add_ps(_mm256_add_ps(a4, a5), _mm256_add_ps(a6, a7)));
    float tmp[8];
    _mm256_storeu_ps(tmp, s);
    sink_f32 = tmp[0] + tmp[1] + tmp[2] + tmp[3] +
               tmp[4] + tmp[5] + tmp[6] + tmp[7];
}
#else
#define FLOPS_PER_ITER 16.0

static void fma_loop(uint64_t iters)
{
    float a0 = 1.0f, a1 = 2.0f, a2 = 3.0f, a3 = 4.0f;
    float a4 = 5.0f, a5 = 6.0f, a6 = 7.0f, a7 = 8.0f;
    const float b = 1.0000001f, c = 1e-7f;

    for (uint64_t i = 0; i < iters; i++) {
        a0 = fmaf(a0, b, c);
        a1 = fmaf(a1, b, c);
        a2 = fmaf(a2, b, c);
        a3 = fmaf(a3, b, c);
        a4 = fmaf(a4, b, c);
        a5 = fmaf(a5, b, c);
        a6 = fmaf(a6, b, c);
        a7 = fmaf(a7, b, c);
    }
    sink_f32 = a0 + a1 + a2 + a3 + a4 + a5 + a6 + a7;
}
#endif

static double fma_once(uint64_t iters, double *gflops)
{
    double t0 = now_s();
    fma_loop(iters);
    double dt = now_s() - t0;
    *gflops = FLOPS_PER_ITER * (double)iters / dt / 1e9;
    return dt;
}

typedef struct {
    uint64_t iters;
    int *go;
    double seconds;
    int mode; /* 0 = integer, 1 = fp32 fma */
} work;

static void *worker(void *arg)
{
    work *w = (work *)arg;
    while (!__atomic_load_n(w->go, __ATOMIC_ACQUIRE))
        ;
    double t0 = now_s();
    if (w->mode == 0)
        int_loop(w->iters);
    else
        fma_loop(w->iters);
    w->seconds = now_s() - t0;
    return NULL;
}

static double run_parallel(int nthreads, uint64_t iters, int mode,
                           double *aggregate)
{
    pthread_t threads[64];
    work args[64];
    int go = 0;

    if (nthreads < 1)
        nthreads = 1;
    if (nthreads > 64)
        nthreads = 64;

    for (int i = 0; i < nthreads; i++) {
        args[i].iters = iters;
        args[i].go = &go;
        args[i].seconds = 0;
        args[i].mode = mode;
        if (pthread_create(&threads[i], NULL, worker, &args[i]) != 0) {
            fprintf(stderr, "pthread_create failed\n");
            exit(2);
        }
    }

    __atomic_store_n(&go, 1, __ATOMIC_RELEASE);

    double slowest = 0;
    for (int i = 0; i < nthreads; i++) {
        pthread_join(threads[i], NULL);
        if (args[i].seconds > slowest)
            slowest = args[i].seconds;
    }

    double per_iter = (mode == 0) ? 8.0 : FLOPS_PER_ITER;
    *aggregate = (double)nthreads * (double)iters * per_iter / slowest / 1e9;
    return slowest;
}

static void mem_bench(double *read_gbs, double *write_gbs, double *copy_gbs)
{
    size_t bytes = (size_t)MEM_BYTES;
    uint64_t *a = malloc(bytes);
    uint64_t *b = malloc(bytes);

    *read_gbs = *write_gbs = *copy_gbs = 0;
    if (!a || !b)
        goto out;

    size_t nelem = bytes / sizeof(uint64_t);
    memset(a, 0x11, bytes);
    memset(b, 0x22, bytes);

    double best_read = 1e9, best_write = 1e9, best_copy = 1e9;

    for (int r = 0; r < REPS; r++) {
        uint64_t s0 = 0, s1 = 0, s2 = 0, s3 = 0;
        uint64_t s4 = 0, s5 = 0, s6 = 0, s7 = 0;
        double t0 = now_s();
        for (size_t i = 0; i + 8 <= nelem; i += 8) {
            s0 += a[i + 0];
            s1 += a[i + 1];
            s2 += a[i + 2];
            s3 += a[i + 3];
            s4 += a[i + 4];
            s5 += a[i + 5];
            s6 += a[i + 6];
            s7 += a[i + 7];
        }
        double dt = now_s() - t0;
        sink_u64 = s0 ^ s1 ^ s2 ^ s3 ^ s4 ^ s5 ^ s6 ^ s7;
        if (dt < best_read)
            best_read = dt;
    }

    for (int r = 0; r < REPS; r++) {
        double t0 = now_s();
        memset(a, 0x5a, bytes);
        double dt = now_s() - t0;
        if (dt < best_write)
            best_write = dt;
    }

    for (int r = 0; r < REPS; r++) {
        double t0 = now_s();
        memcpy(b, a, bytes);
        double dt = now_s() - t0;
        if (dt < best_copy)
            best_copy = dt;
    }

    *read_gbs = (double)bytes / best_read / 1e9;
    *write_gbs = (double)bytes / best_write / 1e9;
    *copy_gbs = 2.0 * (double)bytes / best_copy / 1e9;

out:
    free(a);
    free(b);
}

int main(void)
{
    char brand[256], model[128];
    sysctl_str("machdep.cpu.brand_string", brand, sizeof(brand));
    sysctl_str("hw.model", model, sizeof(model));

    uint64_t physical = sysctl_u64("hw.physicalcpu");
    uint64_t logical = sysctl_u64("hw.logicalcpu");
    uint64_t freq = sysctl_u64("hw.cpufrequency");
    uint64_t mem = sysctl_u64("hw.memsize");

    double chain_best = 1e9;
    for (int r = 0; r < REPS; r++) {
        double dt = int_chain_once(INT_CHAIN_ITERS);
        if (dt < chain_best)
            chain_best = dt;
    }

    double tp_best = 1e9, tp_gops = 0;
    for (int r = 0; r < REPS; r++) {
        double g = 0;
        double dt = int_tp_once(INT_TP_ITERS, &g);
        if (dt < tp_best) {
            tp_best = dt;
            tp_gops = g;
        }
    }

    double fma_best = 1e9, fma_gflops = 0;
    for (int r = 0; r < REPS; r++) {
        double g = 0;
        double dt = fma_once(FMA_ITERS, &g);
        if (dt < fma_best) {
            fma_best = dt;
            fma_gflops = g;
        }
    }

    double read_gbs, write_gbs, copy_gbs;
    mem_bench(&read_gbs, &write_gbs, &copy_gbs);

    int nthreads = logical > 0 ? (int)logical : 1;
    double mt_int = 0, mt_fma = 0;
    run_parallel(nthreads, MT_ITERS, 0, &mt_int);
    run_parallel(nthreads, MT_FMA_ITERS, 1, &mt_fma);

    printf("{\n");
    printf("  \"cpu\": {\n");
    printf("    \"brand\": ");
    json_str(brand);
    printf(",\n    \"model\": ");
    json_str(model);
    printf(",\n    \"physical\": %llu,\n", (unsigned long long)physical);
    printf("    \"logical\": %llu,\n", (unsigned long long)logical);
    printf("    \"freq_hz\": %llu,\n", (unsigned long long)freq);
    printf("    \"mem_bytes\": %llu,\n", (unsigned long long)mem);
    printf("    \"l1d_bytes\": %llu,\n", (unsigned long long)sysctl_u64("hw.l1dcachesize"));
    printf("    \"l2_bytes\": %llu,\n", (unsigned long long)sysctl_u64("hw.l2cachesize"));
    printf("    \"l3_bytes\": %llu\n", (unsigned long long)sysctl_u64("hw.l3cachesize"));
    printf("  },\n");
    printf("  \"isa\": \"%s\",\n", ISA_NAME);
    printf("  \"single\": {\"int_chain_ns\": %.3f, \"int_chain_gops\": %.3f, "
           "\"int_gops\": %.3f, \"fp32_gflops\": %.3f},\n",
           chain_best * 1e9 / ((double)INT_CHAIN_ITERS * 3.0),
           (double)INT_CHAIN_ITERS * 3.0 / chain_best / 1e9,
           tp_gops, fma_gflops);
    printf("  \"memory\": {\"read_gbs\": %.2f, \"write_gbs\": %.2f, "
           "\"copy_gbs\": %.2f},\n", read_gbs, write_gbs, copy_gbs);
    printf("  \"multi\": {\"threads\": %d, \"int_gops\": %.3f, "
           "\"int_scaling\": %.2f, \"fp32_gflops\": %.3f, "
           "\"fp32_scaling\": %.2f}\n",
           nthreads, mt_int,
           tp_gops > 0 ? mt_int / tp_gops : 0.0,
           mt_fma, fma_gflops > 0 ? mt_fma / fma_gflops : 0.0);
    printf("}\n");
    return 0;
}
