/*
 * Metal GPU benchmark for the macOS runner.
 *
 * Enumerates Metal devices, then (if one exists) runs two compute kernels:
 * a float4 copy to measure device-memory bandwidth and a float4 FMA loop to
 * measure FP32 throughput. Emits one JSON object on stdout.
 */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include <stdio.h>
#include <time.h>

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static const char *kShader =
    "#include <metal_stdlib>\n"
    "using namespace metal;\n"
    "\n"
    "kernel void copy_kernel(device const float4 *in [[buffer(0)]],\n"
    "                        device float4 *out [[buffer(1)]],\n"
    "                        constant uint &n [[buffer(2)]],\n"
    "                        uint gid [[thread_position_in_grid]]) {\n"
    "    if (gid < n) out[gid] = in[gid];\n"
    "}\n"
    "\n"
    "kernel void fma_kernel(device float4 *out [[buffer(0)]],\n"
    "                       constant uint &iters [[buffer(1)]],\n"
    "                       uint gid [[thread_position_in_grid]]) {\n"
    "    float4 a = float4(1.0f + float(gid & 1023u) * 1e-6f);\n"
    "    float4 b = float4(1.0000001f);\n"
    "    float4 c = float4(1e-7f);\n"
    "    for (uint i = 0; i < iters; ++i) a = fma(a, b, c);\n"
    "    out[gid] = a;\n"
    "}\n";

static double submit(id<MTLCommandQueue> queue,
                     void (^encode)(id<MTLComputeCommandEncoder>))
{
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
    encode(enc);
    [enc endEncoding];

    double t0 = now_s();
    [cb commit];
    [cb waitUntilCompleted];
    double wall = now_s() - t0;

    if (cb.error) {
        fprintf(stderr, "gpu: command buffer failed: %s\n",
                cb.error.localizedDescription.UTF8String);
        return -1;
    }

    double gpu = cb.GPUEndTime - cb.GPUStartTime;
    return gpu > 0 ? gpu : wall;
}

static double copy_bench(id<MTLDevice> dev, id<MTLCommandQueue> queue,
                         id<MTLComputePipelineState> pso, NSUInteger nvec,
                         int reps, double *gbs)
{
    id<MTLBuffer> in = [dev newBufferWithLength:nvec * sizeof(float) * 4
                                        options:MTLResourceStorageModePrivate];
    id<MTLBuffer> out = [dev newBufferWithLength:nvec * sizeof(float) * 4
                                         options:MTLResourceStorageModePrivate];
    if (!in || !out) {
        *gbs = 0;
        return -1;
    }

    uint32_t n = (uint32_t)nvec;
    double best = 1e9;
    for (int r = 0; r < reps; r++) {
        double t = submit(queue, ^(id<MTLComputeCommandEncoder> enc) {
            [enc setComputePipelineState:pso];
            [enc setBuffer:in offset:0 atIndex:0];
            [enc setBuffer:out offset:0 atIndex:1];
            [enc setBytes:&n length:sizeof n atIndex:2];
            [enc dispatchThreadgroups:MTLSizeMake((nvec + 255) / 256, 1, 1)
                threadsPerThreadgroup:MTLSizeMake(256, 1, 1)];
        });
        if (t > 0 && t < best)
            best = t;
    }

    *gbs = (double)nvec * 16.0 * 2.0 / best / 1e9;
    return best;
}

static double fma_bench(id<MTLDevice> dev, id<MTLCommandQueue> queue,
                        id<MTLComputePipelineState> pso, NSUInteger nvec,
                        uint32_t iters, int reps, double *gflops)
{
    id<MTLBuffer> out = [dev newBufferWithLength:nvec * sizeof(float) * 4
                                         options:MTLResourceStorageModePrivate];
    if (!out) {
        *gflops = 0;
        return -1;
    }

    double best = 1e9;
    for (int r = 0; r < reps; r++) {
        double t = submit(queue, ^(id<MTLComputeCommandEncoder> enc) {
            [enc setComputePipelineState:pso];
            [enc setBuffer:out offset:0 atIndex:0];
            [enc setBytes:&iters length:sizeof iters atIndex:1];
            [enc dispatchThreadgroups:MTLSizeMake((nvec + 255) / 256, 1, 1)
                threadsPerThreadgroup:MTLSizeMake(256, 1, 1)];
        });
        if (t > 0 && t < best)
            best = t;
    }

    *gflops = (double)nvec * (double)iters * 4.0 * 2.0 / best / 1e9;
    return best;
}

static void emit(NSDictionary *root)
{
    NSError *err = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:root
                                                   options:NSJSONWritingPrettyPrinted
                                                     error:&err];
    if (!data) {
        fprintf(stderr, "gpu: cannot serialize results: %s\n",
                err.localizedDescription.UTF8String);
        return;
    }
    fwrite(data.bytes, 1, data.length, stdout);
    fputc('\n', stdout);
}

int main(void)
{
    @autoreleasepool {
        NSMutableDictionary *root = [NSMutableDictionary dictionary];
        NSArray<id<MTLDevice>> *devices = MTLCopyAllDevices();

        NSMutableArray *infos = [NSMutableArray array];
        for (id<MTLDevice> d in devices) {
            MTLSize tg = d.maxThreadsPerThreadgroup;
            [infos addObject:@{
                @"name": d.name ?: @"",
                @"low_power": @(d.isLowPower),
                @"headless": @(d.isHeadless),
                @"unified_memory": @(d.hasUnifiedMemory),
                @"recommended_max_working_set": @(d.recommendedMaxWorkingSetSize),
                @"max_buffer_length": @(d.maxBufferLength),
                @"max_threads_per_group": @(tg.width),
                @"registry_id": @(d.registryID),
            }];
        }
        root[@"devices"] = infos;
        root[@"available"] = @(devices.count > 0);

        if (devices.count == 0) {
            root[@"reason"] = @"MTLCopyAllDevices returned no devices";
            emit(root);
            return 0;
        }

        id<MTLDevice> dev = devices[0];
        id<MTLCommandQueue> queue = [dev newCommandQueue];

        NSError *err = nil;
        id<MTLLibrary> lib = [dev newLibraryWithSource:[NSString stringWithUTF8String:kShader]
                                               options:nil
                                                 error:&err];
        if (!lib) {
            root[@"error"] = err.localizedDescription ?: @"shader compilation failed";
            emit(root);
            return 0;
        }

        id<MTLFunction> copy_fn = [lib newFunctionWithName:@"copy_kernel"];
        id<MTLFunction> fma_fn = [lib newFunctionWithName:@"fma_kernel"];
        id<MTLComputePipelineState> copy_pso =
            [dev newComputePipelineStateWithFunction:copy_fn error:&err];
        if (!copy_pso) {
            root[@"error"] = err.localizedDescription ?: @"copy pipeline failed";
            emit(root);
            return 0;
        }
        id<MTLComputePipelineState> fma_pso =
            [dev newComputePipelineStateWithFunction:fma_fn error:&err];
        if (!fma_pso) {
            root[@"error"] = err.localizedDescription ?: @"fma pipeline failed";
            emit(root);
            return 0;
        }

        /* 16M float4 = 256 MiB per buffer, 512 MiB of traffic per copy. */
        double copy_gbs = 0;
        double copy_s = copy_bench(dev, queue, copy_pso, 1u << 24, 5, &copy_gbs);

        /* 4M threads x 512 float4 FMAs = 17.2 GFLOP. */
        double fma_gflops = 0;
        double fma_s = fma_bench(dev, queue, fma_pso, 1u << 22, 512, 3, &fma_gflops);

        root[@"copy"] = @{
            @"bytes": @((unsigned long long)(1u << 24) * 16 * 2),
            @"seconds": @(copy_s),
            @"gbs": @(copy_gbs),
        };
        root[@"fma"] = @{
            @"threads": @(1u << 22),
            @"iters": @(512),
            @"flops": @(17.179869184e9),
            @"seconds": @(fma_s),
            @"gflops": @(fma_gflops),
        };

        emit(root);
    }
    return 0;
}
