import tensorflow as tf
from tensorflow.keras import layers


def custom_pad(k_size, d_rate):
    s = k_size + (k_size-1) * (d_rate-1)
    pad_s = (s-1)//2
    pad_e = (s-1) - pad_s
    pad_layer = layers.ZeroPadding2D((pad_s, pad_e))
    return pad_layer


class ConvBlock(layers.Layer):
    def __init__(self, n_filters, k_size=3, stride=1, d_rate=1):
        super(ConvBlock, self).__init__()
        pad_type = "same" if stride == 1 else "valid"

        self.blocks = []
        if stride > 1:
            self.blocks.append(custom_pad(k_size, d_rate))
        self.blocks.append(
            layers.Conv2D(n_filters, k_size, stride, pad_type, use_bias=False, dilation_rate=d_rate)
        )

    def call(self, x):
        for block in self.blocks:
            x = block(x)
        return x


class BasicBlock(layers.Layer):
    def __init__(self, n_filters, k_size=3, stride=1, d_rate=1):
        super(BasicBlock, self).__init__()
        self.blocks = [
            # ConvBlock(n_filters, k_size, stride, d_rate) if pad_block else
            layers.Conv2D(n_filters, k_size, stride, "same", use_bias=False),
            layers.BatchNormalization(),
            layers.ReLU()
        ]

    def call(self, x, training=None):
        for block in self.blocks:
            x = block(x)
        return x


class SeparableConvBlock(layers.Layer):
    def __init__(self, n_filters, k_size=3, stride=1, d_rate=1, activation=False):
        super(SeparableConvBlock, self).__init__()
        pad_type = "same" if stride == 1 else "valid"

        self.blocks = [
            custom_pad(k_size, d_rate) if stride > 1 else None,
            layers.ReLU() if activation is False else None,
            layers.DepthwiseConv2D(k_size, stride, pad_type, use_bias=False, dilation_rate=d_rate),
            layers.BatchNormalization(),
            layers.ReLU() if activation is True else None,
            layers.Conv2D(n_filters, 1, 1, "same", use_bias=False),
            layers.BatchNormalization(),
            layers.ReLU() if activation is True else None
        ]

    def call(self, x, training=None):
        for block in self.blocks:
            if block is not None:
                x = block(x)
        return x


class XceptionBlock(layers.Layer):
    def __init__(self, n_filters, skip_type=None, stride=1, d_rate=1, activation=False, return_skip=False):
        super(XceptionBlock, self).__init__()
        # self.sepconv_1 = SeparableConvBlock(n_filters[0], 3, 1, d_rate, activation)
        # self.sepconv_2 = SeparableConvBlock(n_filters[1], 3, 1, d_rate, activation)
        # self.sepconv_3 = SeparableConvBlock(n_filters[2], 3, stride, d_rate, activation)
        self.sepconv_blocks = [
            SeparableConvBlock(n_filters[i], 3, stride if i==2 else 1, d_rate, activation) for i in range(3)
        ]

        self.skip_type = skip_type
        self.return_skip = return_skip
        self.add = layers.Add()
        if skip_type == "conv":
            self.skipconv = ConvBlock(n_filters[-1], 1, stride)
            self.skipbn = layers.BatchNormalization()

    def call(self, x, training=None):
        residual = x

        for i, block in enumerate(self.sepconv_blocks):
            residual = block(residual)
            if i == 1:
                skip = residual

        if self.skip_type == "conv":
            out = self.skipconv(x)
            out = self.skipbn(out)
            out = self.add([residual, out])
        elif self.skip_type == "sum":
            out = self.add([residual, x])
        elif self.skip_type == None:
            out = residual

        if self.return_skip:
            return out, skip
        else:
            return out


class XceptionBackbone(layers.Layer):
    def __init__(self, output_stride=8):
        super(XceptionBackbone, self).__init__()
        x_stride = 1 if output_stride==8 else 2
        d_rates = (2,2,4) if output_stride==8 else (1,1,2)

        self.entry_flow = [
            BasicBlock(32, 3, 2),
            BasicBlock(64, 3, 1),
            XceptionBlock([128]*3, "conv", 2),
            XceptionBlock([256]*3, "conv", 2, return_skip=True),
            XceptionBlock([728]*3, "conv", x_stride)
        ]
        self.middle_flow = [
            XceptionBlock([728]*3, "sum", 1, d_rates[0]) for _ in range(16)
        ]
        self.exit_flow = [
            XceptionBlock([728,1024,1024], "conv", 1, d_rates[1]),
            XceptionBlock([1536,1536,2048], None, 1, d_rates[2], True)
        ]

    def call(self, x, training=None):
        for i, block in enumerate(self.entry_flow):
            if i == 3:
                x, skip = block(x)
            else:
                x = block(x)

        for block in self.middle_flow:
            x = block(x)

        for block in self.exit_flow:
            x = block(x)
        return x, skip


class ASPP(layers.Layer):
    def __init__(self, output_stride=8):
        super(ASPP, self).__init__()
        d_rates = (12,24,36) if output_stride==8 else (6,12,18)

        self.block_1x1 = BasicBlock(256, 1, 1)
        self.block_3x3_rate_S = SeparableConvBlock(256, d_rate=d_rates[0], activation=True)
        self.block_3x3_rate_M = SeparableConvBlock(256, d_rate=d_rates[1], activation=True)
        self.block_3x3_rate_L = SeparableConvBlock(256, d_rate=d_rates[2], activation=True)
        self.blocks_pooling = [
            layers.GlobalAveragePooling2D(),
            layers.Lambda(lambda x: x[:, tf.newaxis, tf.newaxis, :]),
            BasicBlock(256, 1, 1)
        ]

        self.concat = layers.Concatenate()
        self.concat_1x1 = BasicBlock(256, 1, 1)

    def call(self, x, training=None):
        x1 = self.block_1x1(x)
        x2 = self.block_3x3_rate_S(x)
        x3 = self.block_3x3_rate_M(x)
        x4 = self.block_3x3_rate_L(x)
        x5 = x

        for block in self.blocks_pooling:
            x5 = block(x5)

        f_shape = x._shape_tuple()[1:3]
        x5 = tf.image.resize(x5, f_shape, "bilinear")
        x = self.concat([x1, x2, x3, x4, x5])
        return self.concat_1x1(x)


class DeepLabV3Decoder(layers.Layer):
    def __init__(self, n_classes=1):
        super(DeepLabV3Decoder, self).__init__()
        self.skip_block = BasicBlock(48, 1, 1)
        self.concat = layers.Concatenate()

        self.sepconv1 = SeparableConvBlock(256, activation=True)
        self.sepconv2 = SeparableConvBlock(256, activation=True)
        self.conv = layers.Conv2D(n_classes, 1, 1, "same")

        self.sigmoid = layers.Activation("sigmoid")

    def call(self, x, skip, input_shape, training=None):
        skip_shape = skip._shape_tuple()[1:3]
        x = tf.image.resize(x, skip_shape, "bilinear")

        skip = self.skip_block(skip)

        x = self.concat([x, skip])
        x = self.sepconv1(x)
        x = self.sepconv2(x)
        x = self.conv(x)

        x = tf.image.resize(x, input_shape, "bilinear")
        return self.sigmoid(x)


class DeepLabV3pXc(Model):
    def __init__(self, output_stride=8, n_classes=1):
        super(DeepLabV3pXc, self).__init__()
        self.backbone = XceptionBackbone(output_stride)
        self.aspp = ASPP(output_stride)
        self.decoder = DeepLabV3Decoder(n_classes)

    def call(self, x, training=None):
        input_shape = x._shape_tuple()[1:3]

        x, skip = self.backbone(x)
        x = self.aspp(x)
        x = self.decoder(x, skip, input_shape)
        return x

    def get_summary(self, input_shape=(256,256,3)):
        inputs = Input(input_shape)
        model = Model(inputs, self.call(inputs, False))
        print(model.summary())
