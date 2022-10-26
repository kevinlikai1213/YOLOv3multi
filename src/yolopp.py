# Copyright 2020-2022 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""YOLOv3 based on DarkNet."""
from multiprocessing import reduction
import mindspore as ms
import mindspore.nn as nn
import mindspore.ops as ops

from src.darknet import DarkNet, ResidualBlock
from src.loss import XYLoss, WHLoss, ConfidenceLoss, ClassLoss
from model_utils.config import config as default_config


def _conv_bn_relu(in_channel,
                  out_channel,
                  ksize,
                  stride=1,
                  padding=0,
                  dilation=1,
                  alpha=0.1,
                  momentum=0.9,
                  eps=1e-5,
                  pad_mode="same"):
    """Get a conv2d batchnorm and relu layer"""
    return nn.SequentialCell(
        [nn.Conv2d(in_channel,
                   out_channel,
                   kernel_size=ksize,
                   stride=stride,
                   padding=padding,
                   dilation=dilation,
                   pad_mode=pad_mode),
         nn.BatchNorm2d(out_channel, momentum=momentum, eps=eps),
         nn.LeakyReLU(alpha)]
    )

class DoubleConv(nn.Cell):
    def __init__(self,in_channels,out_channels):
        super(DoubleConv, self).__init__()
        self.conv=nn.SequentialCell([
            nn.Conv2d(in_channels,out_channels,kernel_size=3,stride = 1,padding=0,dilation=1, pad_mode = 'same'),
            nn.BatchNorm2d(out_channels),
            # nn.ReLU(),
            nn.LeakyReLU(0.1),
            nn.Conv2d(out_channels,out_channels,kernel_size=3,stride = 1,padding=0,dilation=1, pad_mode = 'same'),
            nn.BatchNorm2d(out_channels),
            nn.LeakyReLU(0.1)]
        )

    def construct(self,x):
        return self.conv(x)

class FPNUpHead(nn.Cell):
    def __init__(self,out_channels=1,features=[32,64,128,256,512,1024],):
        super(FPNUpHead,self).__init__()
        # reversed_feature = reversed(features)
        self.ups = nn.CellList()
        self.conv1 = nn.SequentialCell([
            DoubleConv(features[-1], features[-2]),
            nn.Conv2dTranspose(features[-2], features[-2],kernel_size=2,stride=2)
        ])# in 1024 out 512

        for feature in reversed(features[:-1]):
            self.ups.append(DoubleConv(feature*2,feature))
            self.ups.append(
                nn.Conv2dTranspose(feature,feature//2,kernel_size=2,stride=2,)
            )

        # self.final_conv=nn.Conv2d(features[0],out_channels,kernel_size=1)
        self.concat = ops.Concat(axis=1)
        # self.sigmoid = ops.Sigmoid()
        self.features = features
    def construct(self, features):
        #  [32, 64, 128, 256, 512, 1024]
        outs = []
        x = self.conv1(features[-1])
        if ops.shape(x)!=ops.shape(features[-2]):
            x = ops.ResizeNearestNeighbor(ops.shape(features[-2])[2:])(x)
        outs.append(x)
        x = self.concat((x,features[-2]))# 1024->512 (512*2)
        
        
        x = self.ups[0](x)
        x = self.ups[1](x)
        if ops.shape(x)!=ops.shape(features[-3]):
            x = ops.ResizeNearestNeighbor(ops.shape(features[-3])[2:])(x)
        outs.append(x)
        x = self.concat((x,features[-3]))# 1024->256 (256*2)

        x = self.ups[2](x)
        x = self.ups[3](x)
        if ops.shape(x)!=ops.shape(features[-4]):
            x = ops.ResizeNearestNeighbor(ops.shape(features[-4])[2:])(x)
        outs.append(x)
        x = self.concat((x,features[-4]))     # 512->128 (128*2)   
        
        x = self.ups[4](x)
        x = self.ups[5](x)
        if ops.shape(x)!=ops.shape(features[-5]):
            x = ops.ResizeNearestNeighbor(ops.shape(features[-5])[2:])(x)
        outs.append(x)
        x = self.concat((x,features[-5])) # 256->64 (64*2)  

        x = self.ups[6](x)
        x = self.ups[7](x)
        if ops.shape(x)!=ops.shape(features[-6]):
            x = ops.ResizeNearestNeighbor(ops.shape(features[-6])[2:])(x)
        outs.append(x)
        x = self.concat((x,features[-6])) # 128->32 (32*2)

        x = self.ups[8](x)
        outs.append(x)

        return outs

class UnetppHead(nn.Cell):
    def __init__(self,out_channels=1,features=[32,64,128],):
        super(UnetppHead,self).__init__()
        # reversed_feature = reversed(features)
        self.upconv1 = nn.SequentialCell([
            DoubleConv(features[2], features[1]),
            nn.Conv2dTranspose(features[1], features[1],kernel_size=2,stride=2)
        ])# in 1024 out 512

        self.paraconv2 = DoubleConv(features[1], features[1])


        self.upconv21 = nn.SequentialCell(
            DoubleConv(features[1], features[0]),
            nn.Conv2dTranspose(features[0], features[0],kernel_size=2,stride=2)            
        )
        self.upconv22 = nn.SequentialCell(
            DoubleConv(features[1]+features[1], features[0]),
            nn.Conv2dTranspose(features[0], features[0],kernel_size=2,stride=2)                   
        )

        self.paraconv31 = DoubleConv(features[0], features[0])

        self.paraconv32 = DoubleConv(2*features[0], features[0])
  
        self.final_conv=nn.SequentialCell([
            DoubleConv(features[0]+features[0], 2*features[0]),
            nn.Conv2d(2*features[0],out_channels,kernel_size=1)])
        self.concat = ops.Concat(axis=1)
        self.sigmoid = ops.Sigmoid()
        self.features = features

    def construct(self, features):
        #  [32, 64, 128]
        
        up1 = self.upconv1(features[2])
        para2 = self.paraconv2(features[1])
        if ops.shape(up1)!=ops.shape(features[1]):
            up1 = ops.ResizeNearestNeighbor(ops.shape(features[1])[2:])(up1)
        if ops.shape(para2)!=ops.shape(features[1]):
            para2 = ops.ResizeNearestNeighbor(ops.shape(features[1])[2:])(para2)            
        up1_para2 = self.concat((up1,para2))# 

        up22 = self.upconv22(up1_para2) #up22
        if ops.shape(up22)!=ops.shape(features[0]):
            up22 = ops.ResizeNearestNeighbor(ops.shape(features[0])[2:])(up22)       
        
        up21 = self.upconv21(features[1])
        para31 = self.paraconv31(features[0])
        if ops.shape(up21)!=ops.shape(features[0]):
            up21 = ops.ResizeNearestNeighbor(ops.shape(features[0])[2:])(up21)
        if ops.shape(para31)!=ops.shape(features[0]):
            para31 = ops.ResizeNearestNeighbor(ops.shape(features[1])[2:])(para31)              
        up21_para31 = self.concat((up21, para31))
        
        para32 = self.paraconv32(up21_para31) #paraconv32
        if ops.shape(para32)!=ops.shape(features[0]):
            para32 = ops.ResizeNearestNeighbor(ops.shape(features[0])[2:])(para32)        
        # road = up22-para32
        # x = self.concat((road,up22))
        x = self.concat((para32,up22))
        x = self.final_conv(x)
        return self.sigmoid(x)



class YoloBlock(nn.Cell):
    """
    YoloBlock for YOLOv3.

    Args:
        in_channels: Integer. Input channel.
        out_chls: Integer. Middle channel.
        out_channels: Integer. Output channel.

    Returns:
        Tuple, tuple of output tensor,(f1,f2,f3).

    Examples:
        YoloBlock(1024, 512, 255)

    """
    def __init__(self, in_channels, out_chls, out_channels):
        super(YoloBlock, self).__init__()
        out_chls_2 = out_chls*2

        self.conv0 = _conv_bn_relu(in_channels, out_chls, ksize=1)
        self.conv1 = _conv_bn_relu(out_chls, out_chls_2, ksize=3)

        self.conv2 = _conv_bn_relu(out_chls_2, out_chls, ksize=1)
        self.conv3 = _conv_bn_relu(out_chls, out_chls_2, ksize=3)

        self.conv4 = _conv_bn_relu(out_chls_2, out_chls, ksize=1)
        self.conv5 = _conv_bn_relu(out_chls, out_chls_2, ksize=3)

        self.conv6 = nn.Conv2d(out_chls_2, out_channels, kernel_size=1, stride=1, has_bias=True)

    def construct(self, x):
        c1 = self.conv0(x)
        c2 = self.conv1(c1)

        c3 = self.conv2(c2)
        c4 = self.conv3(c3)

        c5 = self.conv4(c4)
        c6 = self.conv5(c5)

        out = self.conv6(c6)
        return c5, out


class YOLOv3(nn.Cell):
    """
     YOLOv3 Network.

     Note:
         backbone = darknet53

     Args:
         backbone_shape: List. Darknet output channels shape.
         backbone: Cell. Backbone Network.
         out_channel: Integer. Output channel.

     Returns:
         Tensor, output tensor.

     Examples:
         YOLOv3(backbone_shape=[64, 128, 256, 512, 1024]
                backbone=darknet53(),
                out_channel=255)
     """
    def __init__(self, backbone_shape, backbone, out_channel):
        super(YOLOv3, self).__init__()
        self.out_channel = out_channel
        self.backbone = backbone
        self.backblock0 = YoloBlock(backbone_shape[-1], out_chls=backbone_shape[-2], out_channels=out_channel)

        self.conv1 = _conv_bn_relu(in_channel=backbone_shape[-2], out_channel=backbone_shape[-2]//2, ksize=1)
        self.backblock1 = YoloBlock(in_channels=backbone_shape[-2]+backbone_shape[-3],
                                    out_chls=backbone_shape[-3],
                                    out_channels=out_channel)

        self.conv2 = _conv_bn_relu(in_channel=backbone_shape[-3], out_channel=backbone_shape[-3]//2, ksize=1)
        self.backblock2 = YoloBlock(in_channels=backbone_shape[-3]+backbone_shape[-4],
                                    out_chls=backbone_shape[-4],
                                    out_channels=out_channel)
        self.concat = ops.Concat(axis=1)
        self.unetup = FPNUpHead()
    def construct(self, x):
        # input_shape of x is (batch_size, 3, h, w)
        # feature_map1 is (batch_size, backbone_shape[2], h/8, w/8)
        # feature_map2 is (batch_size, backbone_shape[3], h/16, w/16)
        # feature_map3 is (batch_size, backbone_shape[4], h/32, w/32)
        img_hight = ops.Shape()(x)[2]
        img_width = ops.Shape()(x)[3]

        # feature_map1, feature_map2, feature_map3 = self.backbone(x)
        
        features = self.backbone(x)
        out = self.unetup(features)
        features = [out[5], out[3], out[2], out[1], out[0], features[5]]
        feature_map1, feature_map2, feature_map3 = features[3], features[4], features[5]
        
        con1, big_object_output = self.backblock0(feature_map3)

        con1 = self.conv1(con1)
        ups1 = ops.ResizeNearestNeighbor((img_hight // 16, img_width // 16))(con1)
        con1 = self.concat((ups1, feature_map2))
        con2, medium_object_output = self.backblock1(con1)

        con2 = self.conv2(con2)
        ups2 = ops.ResizeNearestNeighbor((img_hight // 8, img_width // 8))(con2)
        con3 = self.concat((ups2, feature_map1))
        _, small_object_output = self.backblock2(con3)

        return big_object_output, medium_object_output, small_object_output, features


class DetectionBlock(nn.Cell):
    """
     YOLOv3 detection Network. It will finally output the detection result.

     Args:
         scale: Character.
         config: Configuration.
         is_training: Bool, Whether train or not, default True.

     Returns:
         Tuple, tuple of output tensor,(f1,f2,f3).

     Examples:
         DetectionBlock(scale='l',stride=32,config=config)
     """

    def __init__(self, scale, config=None, is_training=True):
        super(DetectionBlock, self).__init__()
        self.config = config
        if scale == 's':
            idx = (0, 1, 2)
        elif scale == 'm':
            idx = (3, 4, 5)
        elif scale == 'l':
            idx = (6, 7, 8)
        else:
            raise KeyError("Invalid scale value for DetectionBlock")
        self.anchors = ms.Tensor([self.config.anchor_scales[i] for i in idx], ms.float32)
        self.num_anchors_per_scale = 3
        self.num_attrib = 4+1+self.config.num_classes
        self.lambda_coord = 1

        self.sigmoid = nn.Sigmoid()
        self.reshape = ops.Reshape()
        self.tile = ops.Tile()
        self.concat = ops.Concat(axis=-1)
        self.conf_training = is_training

    def construct(self, x, input_shape):
        num_batch = ops.Shape()(x)[0]
        grid_size = ops.Shape()(x)[2:4]

        # Reshape and transpose the feature to [n, grid_size[0], grid_size[1], 3, num_attrib]
        prediction = ops.Reshape()(x, (num_batch, self.num_anchors_per_scale, self.num_attrib,
                                       grid_size[0], grid_size[1]))
        prediction = ops.Transpose()(prediction, (0, 3, 4, 1, 2))

        range_x = range(grid_size[1])
        range_y = range(grid_size[0])
        grid_x = ops.Cast()(ops.tuple_to_array(range_x), ms.float32)
        grid_y = ops.Cast()(ops.tuple_to_array(range_y), ms.float32)
        # Tensor of shape [grid_size[0], grid_size[1], 1, 1] representing the coordinate of x/y axis for each grid
        # [batch, gridx, gridy, 1, 1]
        grid_x = self.tile(self.reshape(grid_x, (1, 1, -1, 1, 1)), (1, grid_size[0], 1, 1, 1))
        grid_y = self.tile(self.reshape(grid_y, (1, -1, 1, 1, 1)), (1, 1, grid_size[1], 1, 1))
        # Shape is [grid_size[0], grid_size[1], 1, 2]
        grid = self.concat((grid_x, grid_y))

        box_xy = prediction[:, :, :, :, :2]
        box_wh = prediction[:, :, :, :, 2:4]

        # gridsize1 is x
        # gridsize0 is y
        box_xy = (self.sigmoid(box_xy) + grid) / ops.Cast()(ops.tuple_to_array((grid_size[1],
                                                                                grid_size[0])), ms.float32)
        # box_wh is w->h
        box_wh = ops.Exp()(box_wh) * self.anchors / input_shape

        if self.conf_training:
            return grid, prediction, box_xy, box_wh
        box_confidence = prediction[:, :, :, :, 4:5]
        box_probs = prediction[:, :, :, :, 5:]
        box_confidence = self.sigmoid(box_confidence)
        box_probs = self.sigmoid(box_probs)
        return self.concat((box_xy, box_wh, box_confidence, box_probs))


class Iou(nn.Cell):
    """Calculate the iou of boxes"""
    def __init__(self):
        super(Iou, self).__init__()
        self.min = ops.Minimum()
        self.max = ops.Maximum()

    def construct(self, box1, box2):
        # box1: pred_box [batch, gx, gy, anchors, 1,      4] ->4: [x_center, y_center, w, h]
        # box2: gt_box   [batch, 1,  1,  1,       maxbox, 4]
        # convert to topLeft and rightDown
        box1_xy = box1[:, :, :, :, :, :2]
        box1_wh = box1[:, :, :, :, :, 2:4]
        box1_mins = box1_xy - box1_wh / ops.scalar_to_array(2.0)  # topLeft
        box1_maxs = box1_xy + box1_wh / ops.scalar_to_array(2.0)  # rightDown

        box2_xy = box2[:, :, :, :, :, :2]
        box2_wh = box2[:, :, :, :, :, 2:4]
        box2_mins = box2_xy - box2_wh / ops.scalar_to_array(2.0)
        box2_maxs = box2_xy + box2_wh / ops.scalar_to_array(2.0)

        intersect_mins = self.max(box1_mins, box2_mins)
        intersect_maxs = self.min(box1_maxs, box2_maxs)
        intersect_wh = self.max(intersect_maxs - intersect_mins, ops.scalar_to_array(0.0))
        # ops.squeeze: for effiecient slice
        intersect_area = ops.Squeeze(-1)(intersect_wh[:, :, :, :, :, 0:1]) * \
            ops.Squeeze(-1)(intersect_wh[:, :, :, :, :, 1:2])
        box1_area = ops.Squeeze(-1)(box1_wh[:, :, :, :, :, 0:1]) * ops.Squeeze(-1)(box1_wh[:, :, :, :, :, 1:2])
        box2_area = ops.Squeeze(-1)(box2_wh[:, :, :, :, :, 0:1]) * ops.Squeeze(-1)(box2_wh[:, :, :, :, :, 1:2])
        iou = intersect_area / (box1_area + box2_area - intersect_area)
        # iou : [batch, gx, gy, anchors, maxboxes]
        return iou


class YoloLossBlock(nn.Cell):
    """
    Loss block cell of YOLOV3 network.
    """
    def __init__(self, scale, config=None):
        super(YoloLossBlock, self).__init__()
        self.config = config
        if scale == 's':
            # anchor mask
            idx = (0, 1, 2)
        elif scale == 'm':
            idx = (3, 4, 5)
        elif scale == 'l':
            idx = (6, 7, 8)
        else:
            raise KeyError("Invalid scale value for DetectionBlock")
        self.anchors = ms.Tensor([self.config.anchor_scales[i] for i in idx], ms.float32)
        self.ignore_threshold = ms.Tensor(self.config.ignore_threshold, ms.float32)
        self.concat = ops.Concat(axis=-1)
        self.iou = Iou()
        self.reduce_max = ops.ReduceMax(keep_dims=False)
        self.xy_loss = XYLoss()
        self.wh_loss = WHLoss()
        self.confidenceLoss = ConfidenceLoss()
        self.classLoss = ClassLoss()

    def construct(self, grid, prediction, pred_xy, pred_wh, y_true, gt_box, input_shape):
        # prediction : origin output from yolo
        # pred_xy: (sigmoid(xy)+grid)/grid_size
        # pred_wh: (exp(wh)*anchors)/input_shape
        # y_true : after normalize
        # gt_box: [batch, maxboxes, xyhw] after normalize

        object_mask = y_true[:, :, :, :, 4:5]
        class_probs = y_true[:, :, :, :, 5:]

        grid_shape = ops.Shape()(prediction)[1:3]
        grid_shape = ops.Cast()(ops.tuple_to_array(grid_shape[::-1]), ms.float32)

        pred_boxes = self.concat((pred_xy, pred_wh))
        true_xy = y_true[:, :, :, :, :2] * grid_shape - grid
        true_wh = y_true[:, :, :, :, 2:4]
        true_wh = ops.Select()(ops.Equal()(true_wh, 0.0), ops.Fill()(ops.DType()(true_wh),
                                                                     ops.Shape()(true_wh), 1.0), true_wh)

        true_wh = ops.Log()(true_wh / self.anchors * input_shape)
        # 2-w*h for large picture, use small scale, since small obj need more precise
        box_loss_scale = 2 - y_true[:, :, :, :, 2:3] * y_true[:, :, :, :, 3:4]

        gt_shape = ops.Shape()(gt_box)
        gt_box = ops.Reshape()(gt_box, (gt_shape[0], 1, 1, 1, gt_shape[1], gt_shape[2]))

        # add one more dimension for broadcast
        iou = self.iou(ops.ExpandDims()(pred_boxes, -2), gt_box)
        # gt_box is x,y,h,w after normalize
        # [batch, grid[0], grid[1], num_anchor, num_gt]
        best_iou = self.reduce_max(iou, -1)
        # [batch, grid[0], grid[1], num_anchor]

        # ignore_mask IOU too small
        ignore_mask = best_iou < self.ignore_threshold
        ignore_mask = ops.Cast()(ignore_mask, ms.float32)
        ignore_mask = ops.ExpandDims()(ignore_mask, -1)
        # ignore_mask backpro will cause a lot maximunGrad and minimumGrad time consume.
        # so we turn off its gradient
        ignore_mask = ops.stop_gradient(ignore_mask)

        xy_loss = self.xy_loss(object_mask, box_loss_scale, prediction[:, :, :, :, :2], true_xy)
        wh_loss = self.wh_loss(object_mask, box_loss_scale, prediction[:, :, :, :, 2:4], true_wh)
        confidence_loss = self.confidenceLoss(object_mask, prediction[:, :, :, :, 4:5], ignore_mask)
        class_loss = self.classLoss(object_mask, prediction[:, :, :, :, 5:], class_probs)
        loss = xy_loss + wh_loss + confidence_loss + class_loss
        batch_size = ops.Shape()(prediction)[0]
        return loss / batch_size


class YOLOV3DarkNet53(nn.Cell):
    """
    Darknet based YOLOV3 network.

    Args:
        is_training: Bool. Whether train or not.

    Returns:
        Cell, cell instance of Darknet based YOLOV3 neural network.

    Examples:
        YOLOV3DarkNet53(True)
    """

    def __init__(self, is_training, config=default_config):
        super(YOLOV3DarkNet53, self).__init__()
        self.config = config
        self.keep_detect = self.config.keep_detect
        self.tenser_to_array = ops.TupleToArray()

        # YOLOv3 network
        self.feature_map = YOLOv3(backbone=DarkNet(ResidualBlock, self.config.backbone_layers,
                                                   self.config.backbone_input_shape,
                                                   self.config.backbone_shape,
                                                   detect=True),
                                  backbone_shape=self.config.backbone_shape,
                                  out_channel=self.config.out_channel)

        # prediction on the default anchor boxes
        self.detect_1 = DetectionBlock('l', is_training=is_training, config=self.config)
        self.detect_2 = DetectionBlock('m', is_training=is_training, config=self.config)
        self.detect_3 = DetectionBlock('s', is_training=is_training, config=self.config)

        self.mask_decoder = UnetppHead()

    def construct(self, x):
        """
        backbone_input_shape: [32, 64, 128, 256, 512]
        backbone_shape: [64, 128, 256, 512, 1024]
        backbone_layers: [1, 2, 8, 8, 4]

        """
        input_shape = ops.shape(x)[2:4]
        input_shape = ops.cast(self.tenser_to_array(input_shape), ms.float32)
        big_object_output, medium_object_output, small_object_output, features = self.feature_map(x)
        seg_road = self.mask_decoder(features)
        # [32, 64, 128, 256, 512, 1024]
        
        if not self.keep_detect:
            return big_object_output, medium_object_output, small_object_output, seg_road
        output_big = self.detect_1(big_object_output, input_shape)
        output_me = self.detect_2(medium_object_output, input_shape)
        output_small = self.detect_3(small_object_output, input_shape)
        # big is the final output which has smallest feature map
        return output_big, output_me, output_small, seg_road

class UnetLossBlock(nn.Cell):
    """
    Loss block cell of YOLOV3 network.
    """
    def __init__(self, config=None):
        super(UnetLossBlock, self).__init__()
        # self.sigmoid = ops.Sigmoid()
        # self.loss = ops.SigmoidCrossEntropyWithLogits()
        # self.loss = nn.BCEWithLogitsLoss(reduction='mean')
        self.loss = ops.BinaryCrossEntropy('mean')
    def construct(self, pred, mask):

        pred, mask = pred.flatten(), mask.flatten()
        return self.loss(pred.astype('float32'), mask.astype('float32'), None)


class YoloWithLossCell(nn.Cell):
    """YOLOV3 loss."""
    def __init__(self, network, config=default_config):
        super(YoloWithLossCell, self).__init__()
        self.yolo_network = network
        self.config = config
        self.tenser_to_array = ops.TupleToArray()
        self.loss_big = YoloLossBlock('l', self.config)
        self.loss_me = YoloLossBlock('m', self.config)
        self.loss_small = YoloLossBlock('s', self.config)
        self.unet_loss = UnetLossBlock()

    def construct(self, x, y_true_0, y_true_1, y_true_2, gt_0, gt_1, gt_2, mask):
        input_shape = ops.shape(x)[2:4]
        input_shape = ops.cast(self.tenser_to_array(input_shape), ms.float32)
        yolo_out = self.yolo_network(x)
        loss_l = self.loss_big(*yolo_out[0], y_true_0, gt_0, input_shape)
        loss_m = self.loss_me(*yolo_out[1], y_true_1, gt_1, input_shape)
        loss_s = self.loss_small(*yolo_out[2], y_true_2, gt_2, input_shape)
        loss_unet = self.unet_loss(yolo_out[3], mask)
        loss = loss_l +loss_m + loss_s + loss_unet
        # print(loss_unet)
        # print(loss_unet.mean())
        print(loss_l, loss_m, loss_s,loss_unet, loss)
        return loss
