import os
import tvm
import numpy as np
from scipy import signal
from tvm.contrib import nvcc

import topi
from topi.util import get_const_tuple
from topi.cuda.depthwise_conv2d import schedule_depthwise_conv2d_back_input_nhwc #, schedule_depthwise_conv2d_back_nhwc

TASK = "depthwise_conv2d"
USE_MANUAL_CODE = False

@tvm.register_func
def tvm_callback_cuda_compile(code):
    ptx = nvcc.compile_cuda(code, target="ptx", options=["-arch=sm_37"]) # 37 for k80(ec2 instance)
    return ptx

def write_code(code, fname):
    with open(fname, "w") as f:
        f.write(code)

@tvm.register_func
def tvm_callback_cuda_postproc(code):
    if not os.path.exists("perf"):
        os.mkdir("perf")
    write_code(code, "perf/%s_generated.cu" % TASK)
    if USE_MANUAL_CODE:
        code = open("perf/%s_manual.cu" % TASK).read()
    return code

def test_depthwise_conv2d_back_input_nhwc():
    """You may test different settings."""
    batch = 1
    in_channel = 16
    in_height = 32
    in_width = 32

    channel_multiplier = 2
    filter_height = 5
    filter_width = 5

    stride_h = 3
    stride_w = 3

    padding_h = 2
    padding_w = 2

    out_height = np.int((in_height+2*padding_h-filter_height)/stride_h+1)
    out_width = np.int((in_width+2*padding_w-filter_width)/stride_w+1)
    out_channel = in_channel * channel_multiplier

    ishape = [batch, in_height, in_width, in_channel]
    oshape = [batch, out_height, out_width, out_channel]
    stride = [stride_h, stride_w]
    padding = [padding_h, padding_w]

    Out_grad = tvm.placeholder(oshape, name='Out_grad')
    Filter = tvm.placeholder((filter_height, filter_width, in_channel, channel_multiplier), name='Filter')

    In_grad = topi.nn.depthwise_conv2d_back_input_nhwc(Filter, Out_grad, oshape, ishape, stride, padding)

    schedule = schedule_depthwise_conv2d_back_input_nhwc(In_grad)
    print(tvm.lower(schedule,[Filter, Out_grad, In_grad], simple_mode=True))
    f = tvm.build(schedule, [Filter, Out_grad, In_grad], 'cuda')
    ctx = tvm.gpu(0)

    # launch the kernel
    out_backprop_np = np.random.uniform(size=(batch, out_height, out_width, out_channel)).astype(Out_grad.dtype)
    filter_np = np.random.uniform(size=(filter_height, filter_width, in_channel, channel_multiplier)).astype(Filter.dtype)

    out_backprop_tvm = tvm.nd.array(out_backprop_np, ctx)
    filter_tvm = tvm.nd.array(filter_np, ctx)

    in_backprop_tvm = tvm.nd.array(np.zeros((batch, in_height, in_width, in_channel), dtype=Out_grad.dtype), ctx)

    f(filter_tvm, out_backprop_tvm, in_backprop_tvm)

    with tf.device('/cpu:0'):
        out_backprop_tf = tf.placeholder(tf.float32, oshape)
        filter_tf = tf.placeholder(tf.float32, [filter_height, filter_width, in_channel, channel_multiplier])
        In_shape_tf = tf.constant([batch, in_height, in_width, in_channel])
        depth_conv_out = tf.nn.depthwise_conv2d_native_backprop_input(input_sizes=In_shape_tf,
                                                                      filter=filter_tf,
                                                                      out_backprop=out_backprop_tf,
                                                                      strides=[1,stride_h,stride_w,1],
                                                                      padding='SAME')

        config = tf.ConfigProto()
        sess = tf.Session(config=tf.ConfigProto())
        sess.run(tf.global_variables_initializer())
        output_tf = sess.run(depth_conv_out, feed_dict={out_backprop_tf:out_backprop_np, filter_tf:filter_np})

    np.testing.assert_allclose(output_tf, in_backprop_tvm.asnumpy(), rtol=1e-5)
    print "success"






'''
def test_depthwise_conv2d_nchw():
    """You may test different settings."""
    batch = 1
    in_channel = 256
    in_height = 96
    in_width = 96

    filter_channel = in_channel
    channel_multiplier = 1
    filter_height = 3
    filter_width = 3

    stride_h = 1
    stride_w = 1

    padding = 'SAME' # or 'VALID'

    # Placeholder
    Input = tvm.placeholder((batch, in_channel, in_height, in_width), name='Input')
    Filter = tvm.placeholder((filter_channel, channel_multiplier, filter_height, filter_width), name='Filter')
    Stride = [stride_h, stride_w]
    Scale = tvm.placeholder((in_channel * channel_multiplier,), name='Scale')
    Shift = tvm.placeholder((in_channel * channel_multiplier,), name='Shift')
    # Declare
    DepthwiseConv2d = topi.nn.depthwise_conv2d_nchw(Input, Filter, Stride, padding)
    ScaleShift = topi.nn.scale_shift_nchw(DepthwiseConv2d, Scale, Shift)
    Relu = topi.nn.relu(ScaleShift)
    # Schedule
    s1 = schedule_depthwise_conv2d_nchw(DepthwiseConv2d)
    s2 = schedule_depthwise_conv2d_nchw(ScaleShift)
    s3 = schedule_depthwise_conv2d_nchw(Relu)
    input_np = np.random.uniform(size=get_const_tuple(Input.shape)).astype(Input.dtype)
    filter_np = np.random.uniform(size=get_const_tuple(Filter.shape)).astype(Filter.dtype)
    scale_np = np.random.uniform(size=(in_channel * channel_multiplier)).astype(Scale.dtype)
    shift_np = np.random.uniform(size=(in_channel * channel_multiplier)).astype(Shift.dtype)

    def check_device(device):
        if not tvm.module.enabled(device):
            print("Skip because %s is not enabled" % device)
            return
        ctx = tvm.gpu(0) if device == "cuda" else tvm.cl(0)
        # Build the kernel
        f1 = tvm.build(s1, [Input, Filter, DepthwiseConv2d], device)
        f2 = tvm.build(s2, [Input, Filter, Scale, Shift, ScaleShift], device)
        f3 = tvm.build(s3, [Input, Filter, Scale, Shift, Relu], device)
        # Prepare data
        input_tvm = tvm.nd.array(input_np, ctx)
        filter_tvm = tvm.nd.array(filter_np, ctx)
        scale_tvm = tvm.nd.array(scale_np, ctx)
        shift_tvm = tvm.nd.array(shift_np, ctx)

        depthwise_conv2d_tvm = tvm.nd.array(np.zeros(shape=get_const_tuple(DepthwiseConv2d.shape),dtype=DepthwiseConv2d.dtype), ctx)
        scale_shift_tvm = tvm.nd.array(np.zeros(shape=get_const_tuple(ScaleShift.shape), dtype=ScaleShift.dtype), ctx)
        relu_tvm = tvm.nd.array(np.zeros(shape=get_const_tuple(Relu.shape), dtype=Relu.dtype), ctx)
        # Measure time cost of kernel 1 (depthwise_conv2d)
        timer_1 = f1.time_evaluator(f1.entry_name, ctx, number=1000)
        tcost_1 = timer_1(input_tvm, filter_tvm, depthwise_conv2d_tvm).mean
        # Measure time cost of kernel 2 (depthwise_conv2d + scale_shift)
        timer_2 = f2.time_evaluator(f2.entry_name, ctx, number=1000)
        tcost_2 = timer_2(input_tvm, filter_tvm, scale_tvm, shift_tvm, scale_shift_tvm).mean
        # Measure time cost of kernel 3 (depthwise_conv2d + scale_shift + relu)
        timer_3 = f3.time_evaluator(f3.entry_name, ctx, number=1000)
        tcost_3 = timer_3(input_tvm, filter_tvm, scale_tvm, shift_tvm, relu_tvm).mean
        print("Input shape = " + str(get_const_tuple(Input.shape)))
        print("Filter shape = " + str(get_const_tuple(Filter.shape)))
        print("Stride = (%d, %d)" % (stride_h, stride_w))
        print("padding = %s\n" % padding)
        print("Output shape = " + str(get_const_tuple(DepthwiseConv2d.shape)))
        print("average time cost of 1000 runs (depthwise_conv2d) = %g sec" % tcost_1)
        print("average time cost of 1000 runs (depthwise_conv2d + scale_shift) = %g sec" % tcost_2)
        print("average time cost of 1000 runs (depthwise_conv2d + scale_shift + relu) = %g sec" % tcost_3)
        # correctness
        depthwise_conv2d_scipy = topi.testing.depthwise_conv2d_python_nchw(input_np, filter_np, stride=[stride_h, stride_w], padding=padding)
        scale_shift_scipy = np.zeros(shape=get_const_tuple(ScaleShift.shape))
        for c in range(in_channel * channel_multiplier):
            scale_shift_scipy[:,c,:,:] = depthwise_conv2d_scipy[:,c,:,:] * scale_np[c] + shift_np[c]
        relu_scipy = np.maximum(scale_shift_scipy, 0)
        np.testing.assert_allclose(depthwise_conv2d_tvm.asnumpy(), depthwise_conv2d_scipy, rtol=1e-5)
        np.testing.assert_allclose(scale_shift_tvm.asnumpy(), scale_shift_scipy, rtol=1e-5)
        np.testing.assert_allclose(relu_tvm.asnumpy(), relu_scipy, rtol=1e-5)
        print("success")

    with tvm.build_config(auto_unroll_max_step=32,
                          auto_unroll_min_depth=0,
                          unroll_explicit=False,
                          detect_global_barrier=False,
                          restricted_func=True):
        check_device("cuda")
'''
if __name__ == "__main__":
    test_depthwise_conv2d_back_input_nhwc()
