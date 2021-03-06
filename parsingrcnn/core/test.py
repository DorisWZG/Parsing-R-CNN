# Written by Roy Tseng
#
# Based on:
# --------------------------------------------------------
# Copyright (c) 2017-present, Facebook, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##############################################################################
#
# Based on:
# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from collections import defaultdict
import cv2
import numpy as np
import pycocotools.mask as mask_util

from torch.autograd import Variable
import torch

from parsingrcnn.core.config import cfg
from parsingrcnn.utils.timer import Timer
import parsingrcnn.utils.boxes as box_utils
import parsingrcnn.utils.blob as blob_utils
import parsingrcnn.utils.fpn as fpn_utils
import parsingrcnn.utils.image as image_util
import parsingrcnn.utils.keypoints as keypoint_utils
import parsingrcnn.utils.parsing as parsing_utils
import parsingrcnn.core.test_retinanet as test_retinanet


def im_detect_all(model, im, box_proposals=None, timers=None):
    """Process the outputs of model for testing
    Args:
      model: the network module
      im_data: Pytorch variable. Input batch to the model.
      im_info: Pytorch variable. Input batch to the model.
      gt_boxes: Pytorch variable. Input batch to the model.
      num_boxes: Pytorch variable. Input batch to the model.
      args: arguments from command line.
      timer: record the cost of time for different steps
    The rest of inputs are of type pytorch Variables and either input to or output from the model.
    """
    if timers is None:
        timers = defaultdict(Timer)

    if cfg.RETINANET.RETINANET_ON:
        cls_boxes = test_retinanet.im_detect_bbox(model, im, timers)
        return cls_boxes, None, None, None
    
    timers['im_detect_bbox'].tic()
    if cfg.TEST.BBOX_AUG.ENABLED:
        scores, boxes, im_scale, blob_conv = im_detect_bbox_aug(
            model, im, box_proposals)
    else:
        scores, boxes, im_scale, blob_conv = im_detect_bbox(
            model, im, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE, box_proposals)
    timers['im_detect_bbox'].toc()

    # score and boxes are from the whole image after score thresholding and nms
    # (they are not separated by class) (numpy.ndarray)
    # cls_boxes boxes and scores are separated by class and in the format used
    # for evaluating results
    timers['misc_bbox'].tic()
    scores, boxes, cls_boxes = box_results_with_nms_and_limit(scores, boxes)
    timers['misc_bbox'].toc()

    if cfg.MODEL.MASK_ON and boxes.shape[0] > 0:
        timers['im_detect_mask'].tic()
        if cfg.TEST.MASK_AUG.ENABLED:
            masks = im_detect_mask_aug(model, im, boxes, im_scale, blob_conv)
        else:
            masks = im_detect_mask(model, im_scale, boxes, blob_conv)
        timers['im_detect_mask'].toc()

        timers['misc_mask'].tic()
        cls_segms = segm_results(cls_boxes, masks, boxes, im.shape[0], im.shape[1])
        timers['misc_mask'].toc()
    else:
        cls_segms = None

    if cfg.MODEL.KEYPOINTS_ON and boxes.shape[0] > 0:
        timers['im_detect_keypoints'].tic()
        if cfg.TEST.KPS_AUG.ENABLED:
            heatmaps = im_detect_keypoints_aug(model, im, boxes, im_scale, blob_conv)
        else:
            heatmaps = im_detect_keypoints(model, im_scale, boxes, blob_conv)
        timers['im_detect_keypoints'].toc()

        timers['misc_keypoints'].tic()
        cls_keyps = keypoint_results(cls_boxes, heatmaps, boxes)
        timers['misc_keypoints'].toc()
    else:
        cls_keyps = None

    if cfg.MODEL.PARSING_ON and boxes.shape[0] > 0:
        timers['im_detect_parsing'].tic()
        if cfg.TEST.PARSING_AUG.ENABLED:
            parsing = im_detect_parsing_aug(model, im, boxes)
        else:
            parsing = im_detect_parsing(model, im_scale, boxes, blob_conv)
        timers['im_detect_parsing'].toc()

        timers['misc_parsing'].tic()
        cls_parsings = parsing_results(parsing, cls_boxes, im.shape[0], im.shape[1])
        timers['misc_parsing'].toc()
    else:
        cls_parsings = None

    if cfg.MODEL.UV_ON and boxes.shape[0] > 0:
        timers['im_detect_uv'].tic()
        if cfg.TEST.UV_AUG.ENABLED:
            bodys = im_detect_uv_aug(model, im, boxes)
        else:
            bodys = im_detect_uv(model, im_scale, boxes, blob_conv)
        timers['im_detect_uv'].toc()

        timers['misc_uv'].tic()
        cls_uvs = uv_results(model, bodys, boxes)
        timers['misc_uv'].toc()
               
    else:
        cls_uvs = None

    return cls_boxes, cls_segms, cls_keyps, cls_parsings, cls_uvs


def im_conv_body_only(model, im, target_scale, target_max_size):
    inputs, im_scale = _get_blobs(im, None, target_scale, target_max_size)

    if cfg.PYTORCH_VERSION_LESS_THAN_040:
        inputs['data'] = Variable(torch.from_numpy(inputs['data']), volatile=True).cuda()
    else:
        inputs['data'] = torch.from_numpy(inputs['data']).cuda()
    inputs.pop('im_info')

    blob_conv = model.module.convbody_net(**inputs)

    return blob_conv, im_scale


def im_detect_bbox(model, im, target_scale, target_max_size, boxes=None):
    """Prepare the bbox for testing"""

    inputs, im_scale = _get_blobs(im, boxes, target_scale, target_max_size)

    if cfg.DEDUP_BOXES > 0 and not cfg.MODEL.FASTER_RCNN:
        v = np.array([1, 1e3, 1e6, 1e9, 1e12])
        hashes = np.round(inputs['rois'] * cfg.DEDUP_BOXES).dot(v)
        _, index, inv_index = np.unique(
            hashes, return_index=True, return_inverse=True
        )
        inputs['rois'] = inputs['rois'][index, :]
        boxes = boxes[index, :]

    # Add multi-level rois for FPN
    if cfg.FPN.MULTILEVEL_ROIS and not cfg.MODEL.FASTER_RCNN:
        _add_multilevel_rois_for_test(inputs, 'rois')

    if cfg.PYTORCH_VERSION_LESS_THAN_040:
        inputs['data'] = [Variable(torch.from_numpy(inputs['data']), volatile=True)]
        inputs['im_info'] = [Variable(torch.from_numpy(inputs['im_info']), volatile=True)]
    else:
        inputs['data'] = [torch.from_numpy(inputs['data'])]
        inputs['im_info'] = [torch.from_numpy(inputs['im_info'])]

    return_dict = model(**inputs)

    if cfg.MODEL.FASTER_RCNN:
        rois = return_dict['rois'].data.cpu().numpy()
        # unscale back to raw image space
        boxes = rois[:, 1:5] / im_scale

    # cls prob (activations after softmax)
    scores = return_dict['cls_score'].data.cpu().numpy().squeeze()
    # In case there is 1 proposal
    scores = scores.reshape([-1, scores.shape[-1]])

    if cfg.TEST.BBOX_REG:
        # Apply bounding-box regression deltas
        box_deltas = return_dict['bbox_pred'].data.cpu().numpy().squeeze()
        # In case there is 1 proposal
        box_deltas = box_deltas.reshape([-1, box_deltas.shape[-1]])
        if cfg.MODEL.CLS_AGNOSTIC_BBOX_REG:
            # Remove predictions for bg class (compat with MSRA code)
            box_deltas = box_deltas[:, -4:]
        if cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
            # (legacy) Optionally normalize targets by a precomputed mean and stdev
            box_deltas = box_deltas.view(-1, 4) * cfg.TRAIN.BBOX_NORMALIZE_STDS \
                         + cfg.TRAIN.BBOX_NORMALIZE_MEANS
        pred_boxes = box_utils.bbox_transform(boxes, box_deltas, cfg.MODEL.BBOX_REG_WEIGHTS)
        pred_boxes = box_utils.clip_tiled_boxes(pred_boxes, im.shape)
        if cfg.MODEL.CLS_AGNOSTIC_BBOX_REG:
            pred_boxes = np.tile(pred_boxes, (1, scores.shape[1]))
    else:
        # Simply repeat the boxes, once for each class
        pred_boxes = np.tile(boxes, (1, scores.shape[1]))

    if cfg.DEDUP_BOXES > 0 and not cfg.MODEL.FASTER_RCNN:
        # Map scores and predictions back to the original set of boxes
        scores = scores[inv_index, :]
        pred_boxes = pred_boxes[inv_index, :]

    return scores, pred_boxes, im_scale, return_dict['blob_conv']


def im_detect_bbox_aug(model, im, box_proposals=None):
    """Performs bbox detection with test-time augmentations.
    Function signature is the same as for im_detect_bbox.
    """
    assert not cfg.TEST.BBOX_AUG.SCALE_SIZE_DEP, \
        'Size dependent scaling not implemented'
    assert not cfg.TEST.BBOX_AUG.SCORE_HEUR == 'UNION' or \
        cfg.TEST.BBOX_AUG.COORD_HEUR == 'UNION', \
        'Coord heuristic must be union whenever score heuristic is union'
    assert not cfg.TEST.BBOX_AUG.COORD_HEUR == 'UNION' or \
        cfg.TEST.BBOX_AUG.SCORE_HEUR == 'UNION', \
        'Score heuristic must be union whenever coord heuristic is union'
    assert not cfg.MODEL.FASTER_RCNN or \
        cfg.TEST.BBOX_AUG.SCORE_HEUR == 'UNION', \
        'Union heuristic must be used to combine Faster RCNN predictions'

    # Collect detections computed under different transformations
    scores_ts = []
    boxes_ts = []

    def add_preds_t(scores_t, boxes_t):
        scores_ts.append(scores_t)
        boxes_ts.append(boxes_t)

    # Perform detection on the horizontally flipped image
    if cfg.TEST.BBOX_AUG.H_FLIP:
        scores_hf, boxes_hf, _ = im_detect_bbox_hflip(
            model,
            im,
            cfg.TEST.SCALE,
            cfg.TEST.MAX_SIZE,
            box_proposals=box_proposals
        )
        add_preds_t(scores_hf, boxes_hf)

    # Compute detections at different scales
    for scale in cfg.TEST.BBOX_AUG.SCALES:
        max_size = cfg.TEST.BBOX_AUG.MAX_SIZE
        scores_scl, boxes_scl = im_detect_bbox_scale(
            model, im, scale, max_size, box_proposals
        )
        add_preds_t(scores_scl, boxes_scl)

        if cfg.TEST.BBOX_AUG.SCALE_H_FLIP:
            scores_scl_hf, boxes_scl_hf = im_detect_bbox_scale(
                model, im, scale, max_size, box_proposals, hflip=True
            )
            add_preds_t(scores_scl_hf, boxes_scl_hf)

    # Perform detection at different aspect ratios
    for aspect_ratio in cfg.TEST.BBOX_AUG.ASPECT_RATIOS:
        scores_ar, boxes_ar = im_detect_bbox_aspect_ratio(
            model, im, aspect_ratio, box_proposals
        )
        add_preds_t(scores_ar, boxes_ar)

        if cfg.TEST.BBOX_AUG.ASPECT_RATIO_H_FLIP:
            scores_ar_hf, boxes_ar_hf = im_detect_bbox_aspect_ratio(
                model, im, aspect_ratio, box_proposals, hflip=True
            )
            add_preds_t(scores_ar_hf, boxes_ar_hf)

    # Compute detections for the original image (identity transform) last to
    # ensure that the Caffe2 workspace is populated with blobs corresponding
    # to the original image on return (postcondition of im_detect_bbox)
    scores_i, boxes_i, im_scale_i, blob_conv_i = im_detect_bbox(
        model, im, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE, boxes=box_proposals
    )
    add_preds_t(scores_i, boxes_i)

    # Combine the predicted scores
    if cfg.TEST.BBOX_AUG.SCORE_HEUR == 'ID':
        scores_c = scores_i
    elif cfg.TEST.BBOX_AUG.SCORE_HEUR == 'AVG':
        scores_c = np.mean(scores_ts, axis=0)
    elif cfg.TEST.BBOX_AUG.SCORE_HEUR == 'UNION':
        scores_c = np.vstack(scores_ts)
    else:
        raise NotImplementedError(
            'Score heur {} not supported'.format(cfg.TEST.BBOX_AUG.SCORE_HEUR)
        )

    # Combine the predicted boxes
    if cfg.TEST.BBOX_AUG.COORD_HEUR == 'ID':
        boxes_c = boxes_i
    elif cfg.TEST.BBOX_AUG.COORD_HEUR == 'AVG':
        boxes_c = np.mean(boxes_ts, axis=0)
    elif cfg.TEST.BBOX_AUG.COORD_HEUR == 'UNION':
        boxes_c = np.vstack(boxes_ts)
    else:
        raise NotImplementedError(
            'Coord heur {} not supported'.format(cfg.TEST.BBOX_AUG.COORD_HEUR)
        )

    return scores_c, boxes_c, im_scale_i, blob_conv_i


def im_detect_bbox_hflip(
        model, im, target_scale, target_max_size, box_proposals=None):
    """Performs bbox detection on the horizontally flipped image.
    Function signature is the same as for im_detect_bbox.
    """
    # Compute predictions on the flipped image
    im_hf = im[:, ::-1, :]
    im_width = im.shape[1]

    if not cfg.MODEL.FASTER_RCNN:
        box_proposals_hf = box_utils.flip_boxes(box_proposals, im_width)
    else:
        box_proposals_hf = None

    scores_hf, boxes_hf, im_scale, _ = im_detect_bbox(
        model, im_hf, target_scale, target_max_size, boxes=box_proposals_hf
    )

    # Invert the detections computed on the flipped image
    boxes_inv = box_utils.flip_boxes(boxes_hf, im_width)

    return scores_hf, boxes_inv, im_scale


def im_detect_bbox_scale(
        model, im, target_scale, target_max_size, box_proposals=None, hflip=False):
    """Computes bbox detections at the given scale.
    Returns predictions in the original image space.
    """
    if hflip:
        scores_scl, boxes_scl, _ = im_detect_bbox_hflip(
            model, im, target_scale, target_max_size, box_proposals=box_proposals
        )
    else:
        scores_scl, boxes_scl, _, _ = im_detect_bbox(
            model, im, target_scale, target_max_size, boxes=box_proposals
        )
    return scores_scl, boxes_scl


def im_detect_bbox_aspect_ratio(
        model, im, aspect_ratio, box_proposals=None, hflip=False):
    """Computes bbox detections at the given width-relative aspect ratio.
    Returns predictions in the original image space.
    """
    # Compute predictions on the transformed image
    im_ar = image_utils.aspect_ratio_rel(im, aspect_ratio)

    if not cfg.MODEL.FASTER_RCNN:
        box_proposals_ar = box_utils.aspect_ratio(box_proposals, aspect_ratio)
    else:
        box_proposals_ar = None

    if hflip:
        scores_ar, boxes_ar, _ = im_detect_bbox_hflip(
            model,
            im_ar,
            cfg.TEST.SCALE,
            cfg.TEST.MAX_SIZE,
            box_proposals=box_proposals_ar
        )
    else:
        scores_ar, boxes_ar, _, _ = im_detect_bbox(
            model,
            im_ar,
            cfg.TEST.SCALE,
            cfg.TEST.MAX_SIZE,
            boxes=box_proposals_ar
        )

    # Invert the detected boxes
    boxes_inv = box_utils.aspect_ratio(boxes_ar, 1.0 / aspect_ratio)

    return scores_ar, boxes_inv


def im_detect_mask(model, im_scale, boxes, blob_conv):
    """Infer instance segmentation masks. This function must be called after
    im_detect_bbox as it assumes that the Caffe2 workspace is already populated
    with the necessary blobs.

    Arguments:
        model (DetectionModelHelper): the detection model to use
        im_scale (list): image blob scales as returned by im_detect_bbox
        boxes (ndarray): R x 4 array of bounding box detections (e.g., as
            returned by im_detect_bbox)
        blob_conv (Variable): base features from the backbone network.

    Returns:
        pred_masks (ndarray): R x K x M x M array of class specific soft masks
            output by the network (must be processed by segm_results to convert
            into hard masks in the original image coordinate space)
    """
    M = cfg.MRCNN.RESOLUTION
    if boxes.shape[0] == 0:
        pred_masks = np.zeros((0, M, M), np.float32)
        return pred_masks

    inputs = {'mask_rois': _get_rois_blob(boxes, im_scale)}

    # Add multi-level rois for FPN
    if cfg.FPN.MULTILEVEL_ROIS:
        _add_multilevel_rois_for_test(inputs, 'mask_rois')

    pred_masks = model.module.mask_net(blob_conv, inputs)
    pred_masks = pred_masks.data.cpu().numpy().squeeze()

    if cfg.MRCNN.CLS_SPECIFIC_MASK:
        pred_masks = pred_masks.reshape([-1, cfg.MODEL.NUM_CLASSES, M, M])
    else:
        pred_masks = pred_masks.reshape([-1, 1, M, M])

    return pred_masks


def im_detect_mask_aug(model, im, boxes, im_scale, blob_conv):
    """Performs mask detection with test-time augmentations.

    Arguments:
        model (DetectionModelHelper): the detection model to use
        im (ndarray): BGR image to test
        boxes (ndarray): R x 4 array of bounding boxes
        im_scale (list): image blob scales as returned by im_detect_bbox
        blob_conv (Tensor): base features from the backbone network.

    Returns:
        masks (ndarray): R x K x M x M array of class specific soft masks
    """
    assert not cfg.TEST.MASK_AUG.SCALE_SIZE_DEP, \
        'Size dependent scaling not implemented'

    # Collect masks computed under different transformations
    masks_ts = []

    # Compute masks for the original image (identity transform)
    masks_i = im_detect_mask(model, im_scale, boxes, blob_conv)
    masks_ts.append(masks_i)

    # Perform mask detection on the horizontally flipped image
    if cfg.TEST.MASK_AUG.H_FLIP:
        masks_hf = im_detect_mask_hflip(
            model, im, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE, boxes
        )
        masks_ts.append(masks_hf)

    # Compute detections at different scales
    for scale in cfg.TEST.MASK_AUG.SCALES:
        max_size = cfg.TEST.MASK_AUG.MAX_SIZE
        masks_scl = im_detect_mask_scale(model, im, scale, max_size, boxes)
        masks_ts.append(masks_scl)

        if cfg.TEST.MASK_AUG.SCALE_H_FLIP:
            masks_scl_hf = im_detect_mask_scale(
                model, im, scale, max_size, boxes, hflip=True
            )
            masks_ts.append(masks_scl_hf)

    # Compute masks at different aspect ratios
    for aspect_ratio in cfg.TEST.MASK_AUG.ASPECT_RATIOS:
        masks_ar = im_detect_mask_aspect_ratio(model, im, aspect_ratio, boxes)
        masks_ts.append(masks_ar)

        if cfg.TEST.MASK_AUG.ASPECT_RATIO_H_FLIP:
            masks_ar_hf = im_detect_mask_aspect_ratio(
                model, im, aspect_ratio, boxes, hflip=True
            )
            masks_ts.append(masks_ar_hf)

    # Combine the predicted soft masks
    if cfg.TEST.MASK_AUG.HEUR == 'SOFT_AVG':
        masks_c = np.mean(masks_ts, axis=0)
    elif cfg.TEST.MASK_AUG.HEUR == 'SOFT_MAX':
        masks_c = np.amax(masks_ts, axis=0)
    elif cfg.TEST.MASK_AUG.HEUR == 'LOGIT_AVG':

        def logit(y):
            return -1.0 * np.log((1.0 - y) / np.maximum(y, 1e-20))

        logit_masks = [logit(y) for y in masks_ts]
        logit_masks = np.mean(logit_masks, axis=0)
        masks_c = 1.0 / (1.0 + np.exp(-logit_masks))
    else:
        raise NotImplementedError(
            'Heuristic {} not supported'.format(cfg.TEST.MASK_AUG.HEUR)
        )

    return masks_c


def im_detect_mask_hflip(model, im, target_scale, target_max_size, boxes):
    """Performs mask detection on the horizontally flipped image.
    Function signature is the same as for im_detect_mask_aug.
    """
    # Compute the masks for the flipped image
    im_hf = im[:, ::-1, :]
    boxes_hf = box_utils.flip_boxes(boxes, im.shape[1])

    blob_conv, im_scale = im_conv_body_only(model, im_hf, target_scale, target_max_size)
    masks_hf = im_detect_mask(model, im_scale, boxes_hf, blob_conv)

    # Invert the predicted soft masks
    masks_inv = masks_hf[:, :, :, ::-1]

    return masks_inv


def im_detect_mask_scale(
        model, im, target_scale, target_max_size, boxes, hflip=False):
    """Computes masks at the given scale."""
    if hflip:
        masks_scl = im_detect_mask_hflip(
            model, im, target_scale, target_max_size, boxes
        )
    else:
        blob_conv, im_scale = im_conv_body_only(model, im, target_scale, target_max_size)
        masks_scl = im_detect_mask(model, im_scale, boxes, blob_conv)
    return masks_scl


def im_detect_mask_aspect_ratio(model, im, aspect_ratio, boxes, hflip=False):
    """Computes mask detections at the given width-relative aspect ratio."""

    # Perform mask detection on the transformed image
    im_ar = image_utils.aspect_ratio_rel(im, aspect_ratio)
    boxes_ar = box_utils.aspect_ratio(boxes, aspect_ratio)

    if hflip:
        masks_ar = im_detect_mask_hflip(
            model, im_ar, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE, boxes_ar
        )
    else:
        blob_conv, im_scale = im_conv_body_only(
            model, im_ar, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE
        )
        masks_ar = im_detect_mask(model, im_scale, boxes_ar, blob_conv)

    return masks_ar


def im_detect_keypoints(model, im_scale, boxes, blob_conv):
    """Infer instance keypoint poses. This function must be called after
    im_detect_bbox as it assumes that the Caffe2 workspace is already populated
    with the necessary blobs.

    Arguments:
        model (DetectionModelHelper): the detection model to use
        im_scale (list): image blob scales as returned by im_detect_bbox
        boxes (ndarray): R x 4 array of bounding box detections (e.g., as
            returned by im_detect_bbox)

    Returns:
        pred_heatmaps (ndarray): R x J x M x M array of keypoint location
            logits (softmax inputs) for each of the J keypoint types output
            by the network (must be processed by keypoint_results to convert
            into point predictions in the original image coordinate space)
    """
    M = cfg.KRCNN.HEATMAP_SIZE
    if boxes.shape[0] == 0:
        pred_heatmaps = np.zeros((0, cfg.KRCNN.NUM_KEYPOINTS, M, M), np.float32)
        return pred_heatmaps

    inputs = {'keypoint_rois': _get_rois_blob(boxes, im_scale)}

    # Add multi-level rois for FPN
    if cfg.FPN.MULTILEVEL_ROIS:
        _add_multilevel_rois_for_test(inputs, 'keypoint_rois')

    pred_heatmaps = model.module.keypoint_net(blob_conv, inputs)
    pred_heatmaps = pred_heatmaps.data.cpu().numpy().squeeze()

    # In case of 1
    if pred_heatmaps.ndim == 3:
        pred_heatmaps = np.expand_dims(pred_heatmaps, axis=0)

    return pred_heatmaps


def im_detect_keypoints_aug(model, im, boxes, im_scale, blob_conv):
    """Computes keypoint predictions with test-time augmentations.

    Arguments:
        model (DetectionModelHelper): the detection model to use
        im (ndarray): BGR image to test
        boxes (ndarray): R x 4 array of bounding boxes
        im_scale (list): image blob scales as returned by im_detect_bbox
        blob_conv (Tensor): base features from the backbone network.

    Returns:
        heatmaps (ndarray): R x J x M x M array of keypoint location logits
    """
    # Collect heatmaps predicted under different transformations
    heatmaps_ts = []
    # Tag predictions computed under downscaling and upscaling transformations
    ds_ts = []
    us_ts = []

    def add_heatmaps_t(heatmaps_t, ds_t=False, us_t=False):
        heatmaps_ts.append(heatmaps_t)
        ds_ts.append(ds_t)
        us_ts.append(us_t)

    # Compute the heatmaps for the original image (identity transform)
    heatmaps_i = im_detect_keypoints(model, im_scale, boxes, blob_conv)
    add_heatmaps_t(heatmaps_i)

    # Perform keypoints detection on the horizontally flipped image
    if cfg.TEST.KPS_AUG.H_FLIP:
        heatmaps_hf = im_detect_keypoints_hflip(
            model, im, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE, boxes
        )
        add_heatmaps_t(heatmaps_hf)

    # Compute detections at different scales
    for scale in cfg.TEST.KPS_AUG.SCALES:
        ds_scl = scale < cfg.TEST.SCALE
        us_scl = scale > cfg.TEST.SCALE
        heatmaps_scl = im_detect_keypoints_scale(
            model, im, scale, cfg.TEST.KPS_AUG.MAX_SIZE, boxes
        )
        add_heatmaps_t(heatmaps_scl, ds_scl, us_scl)

        if cfg.TEST.KPS_AUG.SCALE_H_FLIP:
            heatmaps_scl_hf = im_detect_keypoints_scale(
                model, im, scale, cfg.TEST.KPS_AUG.MAX_SIZE, boxes, hflip=True
            )
            add_heatmaps_t(heatmaps_scl_hf, ds_scl, us_scl)

    # Compute keypoints at different aspect ratios
    for aspect_ratio in cfg.TEST.KPS_AUG.ASPECT_RATIOS:
        heatmaps_ar = im_detect_keypoints_aspect_ratio(
            model, im, aspect_ratio, boxes
        )
        add_heatmaps_t(heatmaps_ar)

        if cfg.TEST.KPS_AUG.ASPECT_RATIO_H_FLIP:
            heatmaps_ar_hf = im_detect_keypoints_aspect_ratio(
                model, im, aspect_ratio, boxes, hflip=True
            )
            add_heatmaps_t(heatmaps_ar_hf)

    # Select the heuristic function for combining the heatmaps
    if cfg.TEST.KPS_AUG.HEUR == 'HM_AVG':
        np_f = np.mean
    elif cfg.TEST.KPS_AUG.HEUR == 'HM_MAX':
        np_f = np.amax
    else:
        raise NotImplementedError(
            'Heuristic {} not supported'.format(cfg.TEST.KPS_AUG.HEUR)
        )

    def heur_f(hms_ts):
        return np_f(hms_ts, axis=0)

    # Combine the heatmaps
    if cfg.TEST.KPS_AUG.SCALE_SIZE_DEP:
        heatmaps_c = combine_heatmaps_size_dep(
            heatmaps_ts, ds_ts, us_ts, boxes, heur_f
        )
    else:
        heatmaps_c = heur_f(heatmaps_ts)

    return heatmaps_c


def im_detect_keypoints_hflip(model, im, target_scale, target_max_size, boxes):
    """Computes keypoint predictions on the horizontally flipped image.
    Function signature is the same as for im_detect_keypoints_aug.
    """
    # Compute keypoints for the flipped image
    im_hf = im[:, ::-1, :]
    boxes_hf = box_utils.flip_boxes(boxes, im.shape[1])

    blob_conv, im_scale = im_conv_body_only(model, im_hf, target_scale, target_max_size)
    heatmaps_hf = im_detect_keypoints(model, im_scale, boxes_hf, blob_conv)

    # Invert the predicted keypoints
    heatmaps_inv = keypoint_utils.flip_heatmaps(heatmaps_hf)

    return heatmaps_inv


def im_detect_keypoints_scale(
    model, im, target_scale, target_max_size, boxes, hflip=False):
    """Computes keypoint predictions at the given scale."""
    if hflip:
        heatmaps_scl = im_detect_keypoints_hflip(
            model, im, target_scale, target_max_size, boxes
        )
    else:
        blob_conv, im_scale = im_conv_body_only(model, im, target_scale, target_max_size)
        heatmaps_scl = im_detect_keypoints(model, im_scale, boxes, blob_conv)
    return heatmaps_scl


def im_detect_keypoints_aspect_ratio(
    model, im, aspect_ratio, boxes, hflip=False):
    """Detects keypoints at the given width-relative aspect ratio."""

    # Perform keypoint detectionon the transformed image
    im_ar = image_utils.aspect_ratio_rel(im, aspect_ratio)
    boxes_ar = box_utils.aspect_ratio(boxes, aspect_ratio)

    if hflip:
        heatmaps_ar = im_detect_keypoints_hflip(
            model, im_ar, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE, boxes_ar
        )
    else:
        blob_conv, im_scale = im_conv_body_only(
            model, im_ar, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE
        )
        heatmaps_ar = im_detect_keypoints(model, im_scale, boxes_ar, blob_conv)

    return heatmaps_ar


def im_detect_parsing(model, im_scale, boxes, blob_conv):
    """Infer instance segmentation masks. This function must be called after
    im_detect_bbox as it assumes that the Caffe2 workspace is already populated
    with the necessary blobs.

    Arguments:
        model (DetectionModelHelper): the detection model to use
        im_scales (list): image blob scales as returned by im_detect_bbox
        boxes (ndarray): R x 4 array of bounding box detections (e.g., as
            returned by im_detect_bbox)

    Returns:
        parsings (ndarray): R x M x M x k array of class specific soft masks
            output by the network (must be processed by segm_results to convert
            into hard masks in the original image coordinate space)
    """
    M = cfg.PRCNN.RESOLUTION
    if boxes.shape[0] == 0:
        pred_parsing = np.zeros((0, M, M), np.float32)
        return pred_parsing

    inputs = {'parsing_rois': _get_rois_blob(boxes, im_scale)}
    # Add multi-level rois for FPN
    if cfg.FPN.MULTILEVEL_ROIS:
        _add_multilevel_rois_for_test(inputs, 'parsing_rois')

    pred_parsing = model.module.parsing_net(blob_conv, inputs)
    pred_parsing = pred_parsing.data.cpu().numpy().squeeze()

    # In case of 1
    if pred_parsing.ndim == 3:
        pred_parsing = np.expand_dims(pred_parsing, axis=0)

    return pred_parsing


def im_detect_parsing_aug(model, im, boxes):
    """Performs parsing detection with test-time augmentations.

    Arguments:
        model (DetectionModelHelper): the detection model to use
        im (ndarray): BGR image to test
        boxes (ndarray): R x 4 array of bounding boxes

    Returns:
        parsings (ndarray): R x M x M x k array of class specific soft parsings
    """
    assert not cfg.TEST.PARSING_AUG.SCALE_SIZE_DEP, \
        'Size dependent scaling not implemented'

    # Collect parsings computed under different transformations
    parsings_ts = []

    # Compute parsings for the original image (identity transform)
    blob_conv, im_scale_i = im_conv_body_only(
        model, im, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE
    )
    parsings_i = im_detect_parsing(model, im_scale_i, boxes, blob_conv)
    parsings_ts.append(parsings_i)

    # Perform parsing detection on the horizontally flipped image
    if cfg.TEST.PARSING_AUG.H_FLIP:
        parsings_hf = im_detect_parsing_hflip(
            model, im, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE, boxes
        )
        parsings_ts.append(parsings_hf)

    # Compute detections at different scales
    for scale in cfg.TEST.PARSING_AUG.SCALES:
        max_size = cfg.TEST.PARSING_AUG.MAX_SIZE
        parsings_scl = im_detect_parsing_scale(model, im, scale, max_size, boxes)
        parsings_ts.append(parsings_scl)

        if cfg.TEST.PARSING_AUG.SCALE_H_FLIP:
            parsings_scl_hf = im_detect_parsing_scale(
                model, im, scale, max_size, boxes, hflip=True
            )
            parsings_ts.append(parsings_scl_hf)

    # Compute parsings at different aspect ratios
    for aspect_ratio in cfg.TEST.PARSING_AUG.ASPECT_RATIOS:
        parsings_ar = im_detect_parsing_aspect_ratio(model, im, aspect_ratio, boxes)
        parsings_ts.append(parsings_ar)

        if cfg.TEST.PARSING_AUG.ASPECT_RATIO_H_FLIP:
            parsings_ar_hf = im_detect_parsing_aspect_ratio(
                model, im, aspect_ratio, boxes, hflip=True
            )
            parsings_ts.append(parsings_ar_hf)

    # Combine the predicted soft parsings
    if cfg.TEST.PARSING_AUG.HEUR == 'SOFT_AVG':
        parsings_c = np.mean(parsings_ts, axis=0)
    elif cfg.TEST.PARSING_AUG.HEUR == 'SOFT_MAX':
        parsings_c = np.amax(parsings_ts, axis=0)
    elif cfg.TEST.PARSING_AUG.HEUR == 'LOGIT_AVG':

        def logit(y):
            return -1.0 * np.log((1.0 - y) / np.maximum(y, 1e-20))

        logit_parsings = [logit(y) for y in parsings_ts]
        logit_parsings = np.mean(logit_parsings, axis=0)
        parsings_c = 1.0 / (1.0 + np.exp(-logit_parsings))
    else:
        raise NotImplementedError(
            'Heuristic {} not supported'.format(cfg.TEST.PARSING_AUG.HEUR)
        )

    return parsings_c


def im_detect_parsing_hflip(model, im, target_scale, target_max_size, boxes):
    """Performs parsing detection on the horizontally flipped image.
    Function signature is the same as for im_detect_parsing_aug.
    """
    # Compute the parsings for the flipped image
    im_hf = im[:, ::-1, :]
    boxes_hf = box_utils.flip_boxes(boxes, im.shape[1])

    blob_conv, im_scale = im_conv_body_only(
        model, im_hf, target_scale, target_max_size
    )
    parsings_hf = im_detect_parsing(model, im_scale, boxes_hf, blob_conv)

    # Invert the predicted soft parsings
    parsings_inv = parsings_hf[:, :, ::-1, :]
    parsings_inv = parsing_utils.flip_left2right_featuremap(parsings_inv)

    return parsings_inv


def im_detect_parsing_scale(
    model, im, target_scale, target_max_size, boxes, hflip=False
):
    """Computes parsings at the given scale."""
    if hflip:
        parsings_scl = im_detect_parsing_hflip(
            model, im, target_scale, target_max_size, boxes
        )
    else:
        blob_conv, im_scale = im_conv_body_only(
            model, im, target_scale, target_max_size
        )
        parsings_scl = im_detect_parsing(model, im_scale, boxes, blob_conv)
    return parsings_scl


def im_detect_parsing_aspect_ratio(model, im, aspect_ratio, boxes, hflip=False):
    """Computes parsing detections at the given width-relative aspect ratio."""

    # Perform parsing detection on the transformed image
    im_ar = image_utils.aspect_ratio_rel(im, aspect_ratio)
    boxes_ar = box_utils.aspect_ratio(boxes, aspect_ratio)

    if hflip:
        parsings_ar = im_detect_parsing_hflip(
            model, im_ar, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE, boxes_ar
        )
    else:
        blob_conv, im_scale = im_conv_body_only(
            model, im_ar, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE
        )
        parsings_ar = im_detect_parsing(model, im_scale, boxes_ar, blob_conv)

    return parsings_ar


def im_detect_uv(model, im_scale, boxes, blob_conv):
    """Compute uv predictions."""
    M = cfg.PRCNN.RESOLUTION
    if boxes.shape[0] == 0:
        pred_uvs = np.zeros((0, M, M), np.float32)
        return pred_uvs

    inputs = {'uv_rois': _get_rois_blob(boxes, im_scale)}
    # Add multi-level rois for FPN
    if cfg.FPN.MULTILEVEL_ROIS:
        _add_multilevel_rois_for_test(inputs, 'uv_rois')

    AnnIndex, Index_UV, U_uv, V_uv = model.module.UV_net(blob_conv, inputs)
    AnnIndex = AnnIndex.data.cpu().numpy().squeeze()
    Index_UV = Index_UV.data.cpu().numpy().squeeze()
    U_uv = U_uv.data.cpu().numpy().squeeze()
    V_uv = V_uv.data.cpu().numpy().squeeze()

    # In case of 1
    if AnnIndex.ndim == 3:
        AnnIndex = np.expand_dims(AnnIndex, axis=0)
    if Index_UV.ndim == 3:
        Index_UV = np.expand_dims(Index_UV, axis=0)
    if U_uv.ndim == 3:
        U_uv = np.expand_dims(U_uv, axis=0)
    if V_uv.ndim == 3:
        V_uv = np.expand_dims(V_uv, axis=0)

    bodys = [AnnIndex, Index_UV, U_uv, V_uv]

    return bodys


def bodys_append(bodys_ts, bodys_i):
    for i in xrange(len(bodys_ts)):
        bodys_ts[i].append(bodys_i[i])

    return bodys_ts


def im_detect_uv_aug(model, im, boxes):
    """Performs parsing detection with test-time augmentations.

    Arguments:
        model (DetectionModelHelper): the detection model to use
        im (ndarray): BGR image to test
        boxes (ndarray): R x 4 array of bounding boxes

    Returns:
        bodys (list): 
    """
    assert not cfg.TEST.UV_AUG.SCALE_SIZE_DEP, \
        'Size dependent scaling not implemented'

    # Collect parsings computed under different transformations
    bodys_ts = [[] for i in xrange(4)]

    # Compute parsings for the original image (identity transform)
    blob_conv, im_scale_i = im_conv_body_only(
        model, im, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE
    )
    bodys_i = im_detect_uv(model, im_scale_i, boxes, blob_conv)
    bodys_ts = bodys_append(bodys_ts, bodys_i)

    # Perform parsing detection on the horizontally flipped image
    if cfg.TEST.UV_AUG.H_FLIP:
        bodys_hf = im_detect_uv_hflip(
            model, im, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE, boxes
        )
        bodys_ts = bodys_append(bodys_ts, bodys_hf)

    # Compute detections at different scales
    for scale in cfg.TEST.UV_AUG.SCALES:
        max_size = cfg.TEST.UV_AUG.MAX_SIZE
        bodys_scl = im_detect_uv_scale(model, im, scale, max_size, boxes)
        bodys_ts = bodys_append(bodys_ts, bodys_scl)

        if cfg.TEST.UV_AUG.SCALE_H_FLIP:
            bodys_scl_hf = im_detect_uv_scale(
                model, im, scale, max_size, boxes, hflip=True
            )
            bodys_ts = bodys_append(bodys_ts, bodys_scl_hf)

    # Compute bodys at different aspect ratios
    for aspect_ratio in cfg.TEST.UV_AUG.ASPECT_RATIOS:
        bodys_ar = im_detect_uv_aspect_ratio(model, im, aspect_ratio, boxes)
        bodys_ts = bodys_append(bodys_ts, bodys_ar)

        if cfg.TEST.UV_AUG.ASPECT_RATIO_H_FLIP:
            bodys_ar_hf = im_detect_uv_aspect_ratio(
                model, im, aspect_ratio, boxes, hflip=True
            )
            bodys_ts = bodys_append(bodys_ts, bodys_ar_hf)

    # Combine the predicted soft bodys
    bodys_c = []
    if cfg.TEST.UV_AUG.HEUR == 'SOFT_AVG':
        for i in xrange(len(bodys_ts)):
            bodys_c.append(np.mean(bodys_ts[i], axis=0))
    elif cfg.TEST.UV_AUG.HEUR == 'SOFT_MAX':
        for i in xrange(len(bodys_ts)):
            bodys_c.append(np.amax(bodys_ts[i], axis=0))
    else:
        raise NotImplementedError(
            'Heuristic {} not supported'.format(cfg.TEST.UV_AUG.HEUR)
        )

    return bodys_c


def im_detect_uv_hflip(model, im, target_scale, target_max_size, boxes):
    """Performs parsing detection on the horizontally flipped image.
    Function signature is the same as for im_detect_parsing_aug.
    """
    # Compute the parsings for the flipped image
    im_hf = im[:, ::-1, :]
    boxes_hf = box_utils.flip_boxes(boxes, im.shape[1])

    blob_conv, im_scale = im_conv_body_only(
        model, im_hf, target_scale, target_max_size
    )
    bodys_hf = im_detect_uv(model, im_scale, boxes_hf, blob_conv)

    # Invert the predicted soft uv
    bodys_inv = []
    _index = [0,1,2,4,3,6,5,8,7,10,9,12,11,14,13,16,15,18,17,20,19,22,21,24,23]
    label_index = [0,1,3,2,5,4,7,6,9,8,11,10,13,12,14,15,16,17,18,19,20,21,22,23,24]
    UV_symmetry_filename = os.path.join(
        os.path.dirname(__file__), 
        '../../data/DensePoseData/UV_data/UV_symmetry_transforms.mat'
    )
    UV_sym = loadmat(UV_symmetry_filename)
    
    for i in xrange(len(bodys_hf)):
        bodys_hf[i] = bodys_hf[i][:, :, :, ::-1]
    
    bodys_inv.append(bodys_hf[0][:, label_index, :, :])
    bodys_inv.append(bodys_hf[1][:, _index, :, :])

    U_uv, V_uv = bodys_hf[2:]
    U_sym = np.zeros(U_uv.shape)
    V_sym = np.zeros(V_uv.shape)
    U_uv = np.where(U_uv > 1, 1, U_uv)
    V_uv = np.where(V_uv > 1, 1, V_uv)
    U_loc = (U_uv * 255).astype(np.int64)
    V_loc = (V_uv * 255).astype(np.int64)
    for i in xrange(1, 25):
        for j in xrange(len(V_sym)):
            V_sym[j, i] = UV_sym['V_transforms'][0, i - 1][V_loc[j, i],U_loc[j, i]]
            U_sym[j, i] = UV_sym['U_transforms'][0, i - 1][V_loc[j, i],U_loc[j, i]]

    bodys_inv.append(U_sym[:, _index, :, :])
    bodys_inv.append(V_sym[:, _index, :, :])

    return bodys_inv


def im_detect_uv_scale(
    model, im, target_scale, target_max_size, boxes, hflip=False
):
    """Computes parsings at the given scale."""
    if hflip:
        bodys_scl = im_detect_uv_hflip(
            model, im, target_scale, target_max_size, boxes
        )
    else:
        blob_conv, im_scale = im_conv_body_only(
            model, im, target_scale, target_max_size
        )
        bodys_scl = im_detect_uv(model, im_scale, boxes, blob_conv)
    return bodys_scl


def im_detect_uv_aspect_ratio(model, im, aspect_ratio, boxes, hflip=False):
    """Computes parsing detections at the given width-relative aspect ratio."""

    # Perform parsing detection on the transformed image
    im_ar = image_utils.aspect_ratio_rel(im, aspect_ratio)
    boxes_ar = box_utils.aspect_ratio(boxes, aspect_ratio)

    if hflip:
        bodys_ar = im_detect_uv_hflip(
            model, im_ar, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE, boxes_ar
        )
    else:
        blob_conv, im_scale = im_conv_body_only(
            model, im_ar, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE
        )
        bodys_ar = im_detect_uv(model, im_scale, boxes_ar, blob_conv)

    return bodys_ar


def im_detect_fseg(model, im):
    blob_conv, im_scale = im_conv_body_only(
            model, im, cfg.TEST.SCALE, cfg.TEST.MAX_SIZE
        )

    pred_fseg = model.module.fseg_net(blob_conv)
    pred_fseg = pred_fseg.data.cpu().numpy().squeeze()

    # preds axes are CHW; bring p axes to WHC
    pred_fseg = np.swapaxes(pred_fseg, 0, 2)

    # Resize p from (HEATMAP_SIZE, HEATMAP_SIZE, c) to (int(bx), int(by), c)
    pred_fseg = cv2.resize(pred_fseg, (im.shape[0], im.shape[1]))

    # Bring Preds axes back to CHW
    pred_fseg = np.swapaxes(pred_fseg, 0, 2)

    pred_fseg = np.argmax(pred_fseg, axis=0)

    return pred_fseg


def combine_heatmaps_size_dep(hms_ts, ds_ts, us_ts, boxes, heur_f):
    """Combines heatmaps while taking object sizes into account."""
    assert len(hms_ts) == len(ds_ts) and len(ds_ts) == len(us_ts), \
        'All sets of hms must be tagged with downscaling and upscaling flags'

    # Classify objects into small+medium and large based on their box areas
    areas = box_utils.boxes_area(boxes)
    sm_objs = areas < cfg.TEST.KPS_AUG.AREA_TH
    l_objs = areas >= cfg.TEST.KPS_AUG.AREA_TH

    # Combine heatmaps computed under different transformations for each object
    hms_c = np.zeros_like(hms_ts[0])

    for i in range(hms_c.shape[0]):
        hms_to_combine = []
        for hms_t, ds_t, us_t in zip(hms_ts, ds_ts, us_ts):
            # Discard downscaling predictions for small and medium objects
            if sm_objs[i] and ds_t:
                continue
            # Discard upscaling predictions for large objects
            if l_objs[i] and us_t:
                continue
            hms_to_combine.append(hms_t[i])
        hms_c[i] = heur_f(hms_to_combine)

    return hms_c


def box_results_with_nms_and_limit(scores, boxes):  # NOTE: support single-batch
    """Returns bounding-box detection results by thresholding on scores and
    applying non-maximum suppression (NMS).

    `boxes` has shape (#detections, 4 * #classes), where each row represents
    a list of predicted bounding boxes for each of the object classes in the
    dataset (including the background class). The detections in each row
    originate from the same object proposal.

    `scores` has shape (#detection, #classes), where each row represents a list
    of object detection confidence scores for each of the object classes in the
    dataset (including the background class). `scores[i, j]`` corresponds to the
    box at `boxes[i, j * 4:(j + 1) * 4]`.
    """
    num_classes = cfg.MODEL.NUM_CLASSES
    cls_boxes = [[] for _ in range(num_classes)]
    # Apply threshold on detection probabilities and apply NMS
    # Skip j = 0, because it's the background class
    for j in range(1, num_classes):
        inds = np.where(scores[:, j] > cfg.TEST.SCORE_THRESH)[0]
        scores_j = scores[inds, j]
        boxes_j = boxes[inds, j * 4:(j + 1) * 4]
        dets_j = np.hstack((boxes_j, scores_j[:, np.newaxis])).astype(np.float32, copy=False)
        if cfg.TEST.SOFT_NMS.ENABLED:
            nms_dets, _ = box_utils.soft_nms(
                dets_j,
                sigma=cfg.TEST.SOFT_NMS.SIGMA,
                overlap_thresh=cfg.TEST.NMS,
                score_thresh=0.0001,
                method=cfg.TEST.SOFT_NMS.METHOD
            )
        else:
            keep = box_utils.nms(dets_j, cfg.TEST.NMS)
            nms_dets = dets_j[keep, :]
        # Refine the post-NMS boxes using bounding-box voting
        if cfg.TEST.BBOX_VOTE.ENABLED:
            nms_dets = box_utils.box_voting(
                nms_dets,
                dets_j,
                cfg.TEST.BBOX_VOTE.VOTE_TH,
                scoring_method=cfg.TEST.BBOX_VOTE.SCORING_METHOD
            )
        cls_boxes[j] = nms_dets

    # Limit to max_per_image detections **over all classes**
    if cfg.TEST.DETECTIONS_PER_IM > 0:
        image_scores = np.hstack(
            [cls_boxes[j][:, -1] for j in range(1, num_classes)]
        )
        if len(image_scores) > cfg.TEST.DETECTIONS_PER_IM:
            image_thresh = np.sort(image_scores)[-cfg.TEST.DETECTIONS_PER_IM]
            for j in range(1, num_classes):
                keep = np.where(cls_boxes[j][:, -1] >= image_thresh)[0]
                cls_boxes[j] = cls_boxes[j][keep, :]

    im_results = np.vstack([cls_boxes[j] for j in range(1, num_classes)])
    boxes = im_results[:, :-1]
    scores = im_results[:, -1]
    return scores, boxes, cls_boxes


def segm_results(cls_boxes, masks, ref_boxes, im_h, im_w):
    num_classes = cfg.MODEL.NUM_CLASSES
    cls_segms = [[] for _ in range(num_classes)]
    mask_ind = 0
    # To work around an issue with cv2.resize (it seems to automatically pad
    # with repeated border values), we manually zero-pad the masks by 1 pixel
    # prior to resizing back to the original image resolution. This prevents
    # "top hat" artifacts. We therefore need to expand the reference boxes by an
    # appropriate factor.
    M = cfg.MRCNN.RESOLUTION
    scale = (M + 2.0) / M
    ref_boxes = box_utils.expand_boxes(ref_boxes, scale)
    ref_boxes = ref_boxes.astype(np.int32)
    padded_mask = np.zeros((M + 2, M + 2), dtype=np.float32)

    # skip j = 0, because it's the background class
    for j in range(1, num_classes):
        segms = []
        for _ in range(cls_boxes[j].shape[0]):
            if cfg.MRCNN.CLS_SPECIFIC_MASK:
                padded_mask[1:-1, 1:-1] = masks[mask_ind, j, :, :]
            else:
                padded_mask[1:-1, 1:-1] = masks[mask_ind, 0, :, :]

            ref_box = ref_boxes[mask_ind, :]
            w = (ref_box[2] - ref_box[0] + 1)
            h = (ref_box[3] - ref_box[1] + 1)
            w = np.maximum(w, 1)
            h = np.maximum(h, 1)

            mask = cv2.resize(padded_mask, (w, h))
            mask = np.array(mask > cfg.MRCNN.THRESH_BINARIZE, dtype=np.uint8)
            im_mask = np.zeros((im_h, im_w), dtype=np.uint8)

            x_0 = max(ref_box[0], 0)
            x_1 = min(ref_box[2] + 1, im_w)
            y_0 = max(ref_box[1], 0)
            y_1 = min(ref_box[3] + 1, im_h)

            im_mask[y_0:y_1, x_0:x_1] = mask[
                (y_0 - ref_box[1]):(y_1 - ref_box[1]), (x_0 - ref_box[0]):(x_1 - ref_box[0])]

            # Get RLE encoding used by the COCO evaluation API
            rle = mask_util.encode(np.array(im_mask[:, :, np.newaxis], order='F'))[0]
            # For dumping to json, need to decode the byte string.
            # https://github.com/cocodataset/cocoapi/issues/70
            rle['counts'] = rle['counts'].decode('ascii')
            segms.append(rle)

            mask_ind += 1

        cls_segms[j] = segms

    assert mask_ind == masks.shape[0]
    return cls_segms


def keypoint_results(cls_boxes, pred_heatmaps, ref_boxes):
    num_classes = cfg.MODEL.NUM_CLASSES
    cls_keyps = [[] for _ in range(num_classes)]
    person_idx = keypoint_utils.get_person_class_index()
    if cfg.KRCNN.FAST_KPS:
        xy_preds = keypoint_utils.fast_heatmaps_to_keypoints(pred_heatmaps, ref_boxes)
    else:
        if cfg.KRCNN.GAUSS_HEATMAP_TEST:
            xy_preds = keypoint_utils.gauss_heatmaps_to_keypoints(pred_heatmaps, ref_boxes)
        else:
            xy_preds = keypoint_utils.heatmaps_to_keypoints(pred_heatmaps, ref_boxes)

    # NMS OKS
    if cfg.KRCNN.NMS_OKS:
        keep = keypoint_utils.nms_oks(xy_preds, ref_boxes, 0.3)
        xy_preds = xy_preds[keep, :, :]
        ref_boxes = ref_boxes[keep, :]
        pred_heatmaps = pred_heatmaps[keep, :, :, :]
        cls_boxes[person_idx] = cls_boxes[person_idx][keep, :]

    kps = [xy_preds[i] for i in range(xy_preds.shape[0])]
    cls_keyps[person_idx] = kps
    return cls_keyps


def parsing_results(parsings, cls_boxes, im_h, im_w):
    parsings = parsings.transpose((0, 2, 3, 1))
    num_classes = cfg.MODEL.NUM_CLASSES
    cls_parsings = [[] for _ in range(num_classes)]
    boxes = cls_boxes[1][:, 0:4]
    M = cfg.PRCNN.RESOLUTION
    N = cfg.PRCNN.NUM_PARSING
    scale = (M + 2.0) / M
    boxes = box_utils.expand_boxes(boxes, scale)
    boxes = boxes.astype(np.int32)
    padded_parsing = np.zeros((M + 2, M + 2, N), dtype=np.float32)

    for i in range(boxes.shape[0]):
        padded_parsing[1:-1, 1:-1] = parsings[i]

        box = boxes[i, :]
        w = box[2] - box[0] + 1
        h = box[3] - box[1] + 1
        w = np.maximum(w, 1)
        h = np.maximum(h, 1)

        parsing = cv2.resize(padded_parsing, (w, h), interpolation=cv2.INTER_LINEAR)
        parsing = np.argmax(parsing, axis=2)
        im_parsing = np.zeros((im_h, im_w), dtype=np.uint8)

        x_0 = max(box[0], 0)
        x_1 = min(box[2] + 1, im_w)
        y_0 = max(box[1], 0)
        y_1 = min(box[3] + 1, im_h)

        im_parsing[y_0:y_1, x_0:x_1] = parsing[
            (y_0 - box[1]):(y_1 - box[1]),
            (x_0 - box[0]):(x_1 - box[0])
        ]

        cls_parsings[1].append(im_parsing)

    return cls_parsings


def uv_results(model, bodys, boxes):
    AnnIndex, Index_UV, U_uv, V_uv = bodys
    K = cfg.UVRCNN.NUM_PATCHES + 1
    outputs = []

    for ind, entry in enumerate(boxes):
        # Compute ref box width and height
        bx = max(entry[2] - entry[0], 1)
        by = max(entry[3] - entry[1], 1)

        # preds[ind] axes are CHW; bring p axes to WHC
        CurAnnIndex = np.swapaxes(AnnIndex[ind], 0, 2)
        CurIndex_UV = np.swapaxes(Index_UV[ind], 0, 2)
        CurU_uv = np.swapaxes(U_uv[ind], 0, 2)
        CurV_uv = np.swapaxes(V_uv[ind], 0, 2)

        # Resize p from (HEATMAP_SIZE, HEATMAP_SIZE, c) to (int(bx), int(by), c)
        CurAnnIndex = cv2.resize(CurAnnIndex, (by, bx))
        CurIndex_UV = cv2.resize(CurIndex_UV, (by, bx))
        CurU_uv = cv2.resize(CurU_uv, (by, bx))
        CurV_uv = cv2.resize(CurV_uv, (by, bx))

        # Bring Cur_Preds axes back to CHW
        CurAnnIndex = np.swapaxes(CurAnnIndex, 0, 2)
        CurIndex_UV = np.swapaxes(CurIndex_UV, 0, 2)
        CurU_uv = np.swapaxes(CurU_uv, 0, 2)
        CurV_uv = np.swapaxes(CurV_uv, 0, 2)

        # Removed squeeze calls due to singleton dimension issues
        CurAnnIndex = np.argmax(CurAnnIndex, axis=0)
        CurIndex_UV = np.argmax(CurIndex_UV, axis=0)
        CurIndex_UV = CurIndex_UV * (CurAnnIndex>0).astype(np.float32)

        output = np.zeros([3, int(by), int(bx)], dtype=np.float32)
        output[0] = CurIndex_UV

        for part_id in range(1, K):
            CurrentU = CurU_uv[part_id]
            CurrentV = CurV_uv[part_id]
            output[1, CurIndex_UV==part_id] = CurrentU[CurIndex_UV==part_id]
            output[2, CurIndex_UV==part_id] = CurrentV[CurIndex_UV==part_id]
        outputs.append(output)

    num_classes = cfg.MODEL.NUM_CLASSES
    cls_uvs = [[] for _ in range(num_classes)]
    person_idx = keypoint_utils.get_person_class_index()
    cls_uvs[person_idx] = outputs

    return cls_uvs

        
def _get_rois_blob(im_rois, im_scale):
    """Converts RoIs into network inputs.

    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        im_scale_factors (list): scale factors as returned by _get_image_blob

    Returns:
        blob (ndarray): R x 5 matrix of RoIs in the image pyramid with columns
            [level, x1, y1, x2, y2]
    """
    rois, levels = _project_im_rois(im_rois, im_scale)
    rois_blob = np.hstack((levels, rois))
    return rois_blob.astype(np.float32, copy=False)


def _project_im_rois(im_rois, scales):
    """Project image RoIs into the image pyramid built by _get_image_blob.

    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        scales (list): scale factors as returned by _get_image_blob

    Returns:
        rois (ndarray): R x 4 matrix of projected RoI coordinates
        levels (ndarray): image pyramid levels used by each projected RoI
    """
    rois = im_rois.astype(np.float, copy=False) * scales
    levels = np.zeros((im_rois.shape[0], 1), dtype=np.int)
    return rois, levels


def _add_multilevel_rois_for_test(blobs, name):
    """Distributes a set of RoIs across FPN pyramid levels by creating new level
    specific RoI blobs.

    Arguments:
        blobs (dict): dictionary of blobs
        name (str): a key in 'blobs' identifying the source RoI blob

    Returns:
        [by ref] blobs (dict): new keys named by `name + 'fpn' + level`
            are added to dict each with a value that's an R_level x 5 ndarray of
            RoIs (see _get_rois_blob for format)
    """
    lvl_min = cfg.FPN.ROI_MIN_LEVEL
    lvl_max = cfg.FPN.ROI_MAX_LEVEL
    lvls = fpn_utils.map_rois_to_fpn_levels(blobs[name][:, 1:5], lvl_min, lvl_max)
    fpn_utils.add_multilevel_roi_blobs(
        blobs, name, blobs[name], lvls, lvl_min, lvl_max
    )


def _get_blobs(im, rois, target_scale, target_max_size):
    """Convert an image and RoIs within that image into network inputs."""
    blobs = {}
    blobs['data'], im_scale, blobs['im_info'] = \
        blob_utils.get_image_blob(im, target_scale, target_max_size)
    if rois is not None:
        blobs['rois'] = _get_rois_blob(rois, im_scale)
    return blobs, im_scale
