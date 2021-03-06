# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import cv2
import glob
import paddle
import numpy as np

import time
from threading import Thread
from queue import Queue

from paddle.vision.transforms import functional as F
from deploy.python.preprocess import preprocess, Resize, NormalizeImage, Permute, PadStride, LetterBoxResize

from ppdet.core.workspace import create
from ppdet.utils.checkpoint import load_weight, load_pretrain_weight
from ppdet.modeling.mot.utils import Detection, get_crops, scale_coords, clip_box
from ppdet.modeling.mot.utils import Timer, load_det_results
from ppdet.modeling.mot import visualization as mot_vis

from ppdet.metrics import Metric, MOTMetric, KITTIMOTMetric
import ppdet.utils.stats as stats

from .callbacks import Callback, ComposeCallback

from ppdet.utils.logger import setup_logger
logger = setup_logger(__name__)

__all__ = ['Tracker']


class VideoCaptureWidget(object):
    def __init__(self, capture=None, buffer_size=20):
        self.queue = Queue(maxsize=buffer_size)
        self.capture = capture
        
        self.thread = Thread(target=self.update, args=())
        self.thread.daemon = True
        self.thread.start()
        self.video_end = False
        
        self.pred_config_preprocess_infos =  [{'target_size': [608, 1088], 'type': 'LetterBoxResize'}, 
                            {'is_scale': True, 'mean': [0, 0, 0], 'std': [1, 1, 1], 'type': 'NormalizeImage'}, {'type': 'Permute'}]

    def update(self):
        # Read the next frame from the video in a different thread
        while True:
            if self.capture.isOpened() and not self.queue.full():
                status, frame = self.capture.read()
                if not status:
                    self.video_end = True
                    break
                
                data = self.preprocess([frame])
                data['ori_image'] = paddle.unsqueeze(paddle.to_tensor(frame),axis=0)
                for k in data:
                    data[k] = paddle.to_tensor(data[k])
                data = data
                self.queue.put(data)
                
            time.sleep(.01)

    def get_frame(self):
        return None if self.queue.empty() and self.video_end else self.queue.get()
    
    
    def decode_image(self, im_file, im_info):
        """read rgb image
        Args:
            im_file (str|np.ndarray): input can be image path or np.ndarray
            im_info (dict): info of image
        Returns:
            im (np.ndarray):  processed image (np.ndarray)
            im_info (dict): info of processed image
        """
        if isinstance(im_file, str):
            with open(im_file, 'rb') as f:
                im_read = f.read()
            data = np.frombuffer(im_read, dtype='uint8')
            im = cv2.imdecode(data, 1)  # BGR mode, but need RGB mode
            im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
        else:
            im = im_file
        im_info['im_shape'] = np.array(im.shape[:2], dtype=np.float32)
        im_info['scale_factor'] = np.array([1., 1.], dtype=np.float32)
        return im, im_info
    
    def preprocess_(self, im, preprocess_ops):
        # process image by preprocess_ops
        im_info = {
            'scale_factor': np.array(
                [1., 1.], dtype=np.float32),
            'im_shape': None,
        }
        im, im_info = self.decode_image(im, im_info)
        for operator in preprocess_ops:
            im, im_info = operator(im, im_info)
        return im, im_info
    
    def create_inputs(self, imgs, im_info):
        """generate input for different model type
        Args:
            imgs (list(numpy)): list of images (np.ndarray)
            im_info (list(dict)): list of image info
        Returns:
            inputs (dict): input of model
        """
        inputs = {}

        im_shape = []
        scale_factor = []
        if len(imgs) == 1:
            inputs['image'] = np.array((imgs[0], )).astype('float32')
            inputs['im_shape'] = np.array(
                (im_info[0]['im_shape'], )).astype('float32')
            inputs['scale_factor'] = np.array(
                (im_info[0]['scale_factor'], )).astype('float32')
            return inputs

        for e in im_info:
            im_shape.append(np.array((e['im_shape'], )).astype('float32'))
            scale_factor.append(np.array((e['scale_factor'], )).astype('float32'))

        inputs['im_shape'] = np.concatenate(im_shape, axis=0)
        inputs['scale_factor'] = np.concatenate(scale_factor, axis=0)

        imgs_shape = [[e.shape[1], e.shape[2]] for e in imgs]
        max_shape_h = max([e[0] for e in imgs_shape])
        max_shape_w = max([e[1] for e in imgs_shape])
        padding_imgs = []
        for img in imgs:
            im_c, im_h, im_w = img.shape[:]
            padding_im = np.zeros(
                (im_c, max_shape_h, max_shape_w), dtype=np.float32)
            padding_im[:, :im_h, :im_w] = img
            padding_imgs.append(padding_im)
        inputs['image'] = np.stack(padding_imgs, axis=0)
        return inputs
    def preprocess(self, image_list):
        preprocess_ops = []
        for op_info in self.pred_config_preprocess_infos:
            new_op_info = op_info.copy()
            op_type = new_op_info.pop('type')
            preprocess_ops.append(eval(op_type)(**new_op_info))

        input_im_lst = []
        input_im_info_lst = []
        for im_path in image_list:
            im, im_info = self.preprocess_(im_path, preprocess_ops)
            input_im_lst.append(im)
            input_im_info_lst.append(im_info)
        inputs = self.create_inputs(input_im_lst, input_im_info_lst)
        return inputs

class Tracker(object):
    def __init__(self, cfg, mode='eval'):
        self.cfg = cfg
        assert mode.lower() in ['test', 'eval'], \
                "mode should be 'test' or 'eval'"
        self.mode = mode.lower()
        self.optimizer = None

        # build MOT data loader
        self.dataset = cfg['{}MOTDataset'.format(self.mode.capitalize())]
        
        self.capture = None
        # build model
        self.model = create(cfg.architecture)

        self.status = {}
        self.start_epoch = 0

        # initial default callbacks
        self._init_callbacks()

        # initial default metrics
        self._init_metrics()
        self._reset_metrics()

    def _init_callbacks(self):
        self._callbacks = []
        self._compose_callback = None

    def _init_metrics(self):
        if self.mode in ['test']:
            self._metrics = []
            return

        if self.cfg.metric == 'MOT':
            self._metrics = [MOTMetric(), ]
        elif self.cfg.metric == 'KITTI':
            self._metrics = [KITTIMOTMetric(), ]
        else:
            logger.warning("Metric not support for metric type {}".format(
                self.cfg.metric))
            self._metrics = []

    def _reset_metrics(self):
        for metric in self._metrics:
            metric.reset()

    def register_callbacks(self, callbacks):
        callbacks = [h for h in list(callbacks) if h is not None]
        for c in callbacks:
            assert isinstance(c, Callback), \
                    "metrics shoule be instances of subclass of Metric"
        self._callbacks.extend(callbacks)
        self._compose_callback = ComposeCallback(self._callbacks)

    def register_metrics(self, metrics):
        metrics = [m for m in list(metrics) if m is not None]
        for m in metrics:
            assert isinstance(m, Metric), \
                    "metrics shoule be instances of subclass of Metric"
        self._metrics.extend(metrics)

    def load_weights_jde(self, weights):
        load_weight(self.model, weights, self.optimizer)

    def load_weights_sde(self, det_weights, reid_weights):
        if self.model.detector:
            load_weight(self.model.detector, det_weights)
            load_weight(self.model.reid, reid_weights)
        else:
            load_weight(self.model.reid, reid_weights, self.optimizer)
            
    def _eval_seq_jde(self,
                      dataloader,
                      save_dir=None,
                      show_image=False,
                      frame_rate=30,
                      draw_threshold=0,
                      capture=None,
                      writer=None):
        if save_dir:
            if not os.path.exists(save_dir): os.makedirs(save_dir)
        tracker = self.model.tracker
        tracker.max_time_lost = int(frame_rate / 30.0 * tracker.track_buffer)

        timer = Timer()
        results = []
        frame_id = 0
        self.status['mode'] = 'track'
        self.model.eval()
        if capture is not None:
            # use cv2 capture
            video_len = capture.get(cv2.CAP_PROP_FRAME_COUNT)
            if video_len <= 0:
                video_len = 200000
            vcw = VideoCaptureWidget(capture, buffer_size=50)
            logger.info("Length of the video: {} frames".format(video_len))
            
            dataloader = range(round(video_len))
        for step_id, data in enumerate(dataloader):
            # forward
            if capture != None:
                # ret, data = capture.read()
                data = vcw.get_frame()
                if data is None:
                    logger.info("Processing video end.")
                    break
            self.status['step_id'] = step_id
            if frame_id % 2000 == 0:
                logger.info('Processing frame {} ({:.2f} fps)'.format(
                    frame_id, 1. / max(1e-5, timer.average_time)))
                
            timer.tic()
            pred_dets, pred_embs = self.model(data)
            online_targets = self.model.tracker.update(pred_dets, pred_embs)

            online_tlwhs, online_ids = [], []
            online_scores = []
            for t in online_targets:
                tlwh = t.tlwh
                tid = t.track_id
                tscore = t.score
                if tscore < draw_threshold: continue
                vertical = tlwh[2] / tlwh[3] > 1.6
                if tlwh[2] * tlwh[3] > tracker.min_box_area and not vertical:
                    online_tlwhs.append(tlwh)
                    online_ids.append(tid)
                    online_scores.append(tscore)
            timer.toc()

            # save results
            results.append(
                (frame_id + 1, online_tlwhs, online_scores, online_ids))
            self.save_results(data, frame_id, online_ids, online_tlwhs,
                            online_scores, timer.average_time, show_image,
                            save_dir, writer=writer)
            frame_id += 1
            

        return results, frame_id, timer.average_time, timer.calls

    def _eval_seq_sde(self,
                      dataloader,
                      save_dir=None,
                      show_image=False,
                      frame_rate=30,
                      scaled=False,
                      det_file='',
                      draw_threshold=0):
        if save_dir:
            if not os.path.exists(save_dir): os.makedirs(save_dir)
        tracker = self.model.tracker
        use_detector = False if not self.model.detector else True

        timer = Timer()
        results = []
        frame_id = 0
        self.status['mode'] = 'track'
        self.model.eval()
        self.model.reid.eval()
        if not use_detector:
            dets_list = load_det_results(det_file, len(dataloader))
            logger.info('Finish loading detection results file {}.'.format(
                det_file))

        for step_id, data in enumerate(dataloader):
            self.status['step_id'] = step_id
            if frame_id % 40 == 0:
                logger.info('Processing frame {} ({:.2f} fps)'.format(
                    frame_id, 1. / max(1e-5, timer.average_time)))

            ori_image = data['ori_image']
            input_shape = data['image'].shape[2:]
            im_shape = data['im_shape']
            scale_factor = data['scale_factor']
            timer.tic()
            if not use_detector:
                dets = dets_list[frame_id]
                bbox_tlwh = paddle.to_tensor(dets['bbox'], dtype='float32')
                pred_scores = paddle.to_tensor(dets['score'], dtype='float32')
                if pred_scores < draw_threshold: continue
                if bbox_tlwh.shape[0] > 0:
                    pred_bboxes = paddle.concat(
                        (bbox_tlwh[:, 0:2],
                         bbox_tlwh[:, 2:4] + bbox_tlwh[:, 0:2]),
                        axis=1)
                else:
                    pred_bboxes = []
                    pred_scores = []
            else:
                outs = self.model.detector(data)
                if outs['bbox_num'] > 0:
                    if not scaled:
                        pred_bboxes = scale_coords(outs['bbox'][:, 2:],
                                                   input_shape, im_shape,
                                                   scale_factor)
                    else:
                        pred_bboxes = outs['bbox'][:, 2:]
                    pred_scores = outs['bbox'][:, 1:2]
                else:
                    pred_bboxes = []
                    pred_scores = []

            pred_bboxes = clip_box(pred_bboxes, input_shape, im_shape,
                                   scale_factor)
            bbox_tlwh = paddle.concat(
                (pred_bboxes[:, 0:2],
                 pred_bboxes[:, 2:4] - pred_bboxes[:, 0:2] + 1),
                axis=1)

            crops, pred_scores = get_crops(
                pred_bboxes, ori_image, pred_scores, w=64, h=192)
            crops = paddle.to_tensor(crops)
            pred_scores = paddle.to_tensor(pred_scores)

            data.update({'crops': crops})
            features = self.model(data)
            features = features.numpy()
            detections = [
                Detection(tlwh, score, feat)
                for tlwh, score, feat in zip(bbox_tlwh, pred_scores, features)
            ]
            self.model.tracker.predict()
            online_targets = self.model.tracker.update(detections)

            online_tlwhs = []
            online_scores = []
            online_ids = []
            for track in online_targets:
                if not track.is_confirmed() or track.time_since_update > 1:
                    continue
                online_tlwhs.append(track.to_tlwh())
                online_scores.append(1.0)
                online_ids.append(track.track_id)
            timer.toc()

            # save results
            results.append(
                (frame_id + 1, online_tlwhs, online_scores, online_ids))
            self.save_results(data, frame_id, online_ids, online_tlwhs,
                              online_scores, timer.average_time, show_image,
                              save_dir)
            frame_id += 1

        return results, frame_id, timer.average_time, timer.calls

    def mot_evaluate(self,
                     data_root,
                     seqs,
                     output_dir,
                     data_type='mot',
                     model_type='JDE',
                     save_images=False,
                     save_videos=False,
                     show_image=False,
                     scaled=False,
                     det_results_dir=''):
        if not os.path.exists(output_dir): os.makedirs(output_dir)
        result_root = os.path.join(output_dir, 'mot_results')
        if not os.path.exists(result_root): os.makedirs(result_root)
        assert data_type in ['mot', 'kitti'], \
            "data_type should be 'mot' or 'kitti'"
        assert model_type in ['JDE', 'DeepSORT', 'FairMOT'], \
            "model_type should be 'JDE', 'DeepSORT' or 'FairMOT'"

        # run tracking

        n_frame = 0
        timer_avgs, timer_calls = [], []
        for seq in seqs:
            if not os.path.isdir(os.path.join(data_root, seq)):
                continue
            infer_dir = os.path.join(data_root, seq, 'img1')
            seqinfo = os.path.join(data_root, seq, 'seqinfo.ini')
            if not os.path.exists(seqinfo) or not os.path.exists(
                    infer_dir) or not os.path.isdir(infer_dir):
                continue

            save_dir = os.path.join(output_dir, 'mot_outputs',
                                    seq) if save_images or save_videos else None
            logger.info('start seq: {}'.format(seq))

            images = self.get_infer_images(infer_dir)
            self.dataset.set_images(images)

            dataloader = create('EvalMOTReader')(self.dataset, 0)

            result_filename = os.path.join(result_root, '{}.txt'.format(seq))
            meta_info = open(seqinfo).read()
            frame_rate = int(meta_info[meta_info.find('frameRate') + 10:
                                       meta_info.find('\nseqLength')])
            with paddle.no_grad():
                if model_type in ['JDE', 'FairMOT']:
                    results, nf, ta, tc = self._eval_seq_jde(
                        dataloader,
                        save_dir=save_dir,
                        show_image=show_image,
                        frame_rate=frame_rate)
                elif model_type in ['DeepSORT']:
                    results, nf, ta, tc = self._eval_seq_sde(
                        dataloader,
                        save_dir=save_dir,
                        show_image=show_image,
                        frame_rate=frame_rate,
                        scaled=scaled,
                        det_file=os.path.join(det_results_dir,
                                              '{}.txt'.format(seq)))
                else:
                    raise ValueError(model_type)

            self.write_mot_results(result_filename, results, data_type)
            n_frame += nf
            timer_avgs.append(ta)
            timer_calls.append(tc)

            if save_videos:
                output_video_path = os.path.join(save_dir, '..',
                                                 '{}_vis.mp4'.format(seq))
                cmd_str = 'ffmpeg -f image2 -i {}/%05d.jpg {}'.format(
                    save_dir, output_video_path)
                os.system(cmd_str)
                logger.info('Save video in {}.'.format(output_video_path))

            logger.info('Evaluate seq: {}'.format(seq))
            # update metrics
            for metric in self._metrics:
                metric.update(data_root, seq, data_type, result_root,
                              result_filename)

        timer_avgs = np.asarray(timer_avgs)
        timer_calls = np.asarray(timer_calls)
        all_time = np.dot(timer_avgs, timer_calls)
        avg_time = all_time / np.sum(timer_calls)
        logger.info('Time elapsed: {:.2f} seconds, FPS: {:.2f}'.format(
            all_time, 1.0 / avg_time))

        # accumulate metric to log out
        for metric in self._metrics:
            metric.accumulate()
            metric.log()
        # reset metric states for metric may performed multiple times
        self._reset_metrics()

    def get_infer_images(self, infer_dir):
        assert infer_dir is None or os.path.isdir(infer_dir), \
            "{} is not a directory".format(infer_dir)
        images = set()
        assert os.path.isdir(infer_dir), \
            "infer_dir {} is not a directory".format(infer_dir)
        exts = ['jpg', 'jpeg', 'png', 'bmp']
        exts += [ext.upper() for ext in exts]
        for ext in exts:
            images.update(glob.glob('{}/*.{}'.format(infer_dir, ext)))
        images = list(images)
        images.sort()
        assert len(images) > 0, "no image found in {}".format(infer_dir)
        logger.info("Found {} inference images in total.".format(len(images)))
        return images

    def mot_predict(self,
                    video_file,
                    frame_rate,
                    image_dir,
                    output_dir,
                    data_type='mot',
                    model_type='JDE',
                    save_images=False,
                    save_videos=True,
                    show_image=False,
                    scaled=False,
                    det_results_dir='',
                    draw_threshold=0.5,
                    use_capture=True):
        assert video_file is not None or image_dir is not None, \
            "--video_file or --image_dir should be set."
        assert video_file is None or os.path.isfile(video_file), \
                "{} is not a file".format(video_file)
        assert image_dir is None or os.path.isdir(image_dir), \
                "{} is not a directory".format(image_dir)

        if not os.path.exists(output_dir): os.makedirs(output_dir)
        result_root = os.path.join(output_dir, 'mot_results')
        if not os.path.exists(result_root): os.makedirs(result_root)
        assert data_type in ['mot', 'kitti'], \
            "data_type should be 'mot' or 'kitti'"
        assert model_type in ['JDE', 'DeepSORT', 'FairMOT'], \
            "model_type should be 'JDE', 'DeepSORT' or 'FairMOT'"

        # run tracking        
        if video_file:
            seq = video_file.split('/')[-1].split('.')[0]
            if use_capture:
                # ??????opencv??????????????????ffmpeg????????????????????????????????????????????????????????????
                self.capture = cv2.VideoCapture(video_file)
            else:
                self.dataset.set_video(video_file, frame_rate)
            logger.info('Starting tracking video {}'.format(video_file))
        elif image_dir:
            seq = image_dir.split('/')[-1].split('.')[0]
            images = [
                '{}/{}'.format(image_dir, x) for x in os.listdir(image_dir)
            ]
            images.sort()
            self.dataset.set_images(images)
            logger.info('Starting tracking folder {}, found {} images'.format(
                image_dir, len(images)))
        else:
            raise ValueError('--video_file or --image_dir should be set.')

        save_dir = os.path.join(output_dir, 'mot_outputs',
                                seq) if save_images or save_videos else None
        
        dataloader = create('TestMOTReader')(self.dataset, 0) if use_capture is False else None
        result_filename = os.path.join(result_root, '{}.txt'.format(seq))
        if frame_rate == -1:
            if self.capture:
                frame_rate = self.capture.get(cv2.CAP_PROP_FPS)
                if frame_rate <= 0 or frame_rate > 1000:
                    frame_rate = 25.01
            else: frame_rate = self.dataset.frame_rate
        video_writer = None
        if save_videos and use_capture:
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            output_video_path = os.path.join(output_dir, '{}_vis.mp4'.format(seq))
            logger.info('Save video in {}'.format(output_video_path))
            width = int(self.capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            video_writer = cv2.VideoWriter(output_video_path, fourcc, frame_rate, (width, height))

        with paddle.no_grad():
            if model_type in ['JDE', 'FairMOT']:
                results, nf, ta, tc = self._eval_seq_jde(
                    dataloader,
                    save_dir=save_dir,
                    show_image=show_image,
                    frame_rate=frame_rate,
                    draw_threshold=draw_threshold,
                    capture=self.capture,
                    writer=video_writer)
            elif model_type in ['DeepSORT']:
                results, nf, ta, tc = self._eval_seq_sde(
                    dataloader,
                    save_dir=save_dir,
                    show_image=show_image,
                    frame_rate=frame_rate,
                    scaled=scaled,
                    det_file=os.path.join(det_results_dir,
                                          '{}.txt'.format(seq)),
                    draw_threshold=draw_threshold)
            else:
                raise ValueError(model_type)

        self.write_mot_results(result_filename, results, data_type)

        if not use_capture and save_videos:
            output_video_path = os.path.join(save_dir, '..',
                                             '{}_vis.mp4'.format(seq))
            cmd_str = 'ffmpeg -f image2 -i {}/%05d.jpg {}'.format(
                save_dir, output_video_path)
            os.system(cmd_str)
            logger.info('Save video in {}'.format(output_video_path))

    def write_mot_results(self, filename, results, data_type='mot'):
        if data_type in ['mot', 'mcmot', 'lab']:
            save_format = '{frame},{id},{x1},{y1},{w},{h},{score},-1,-1,-1\n'
        elif data_type == 'kitti':
            save_format = '{frame} {id} car 0 0 -10 {x1} {y1} {x2} {y2} -10 -10 -10 -1000 -1000 -1000 -10\n'
        else:
            raise ValueError(data_type)

        with open(filename, 'w') as f:
            for frame_id, tlwhs, tscores, track_ids in results:
                if data_type == 'kitti':
                    frame_id -= 1
                for tlwh, score, track_id in zip(tlwhs, tscores, track_ids):
                    if track_id < 0:
                        continue
                    x1, y1, w, h = tlwh
                    x2, y2 = x1 + w, y1 + h
                    line = save_format.format(
                        frame=frame_id,
                        id=track_id,
                        x1=x1,
                        y1=y1,
                        x2=x2,
                        y2=y2,
                        w=w,
                        h=h,
                        score=score)
                    f.write(line)
        logger.info('MOT results save in {}'.format(filename))

    def save_results(self, data, frame_id, online_ids, online_tlwhs,
                     online_scores, average_time, show_image, save_dir, writer=None):
        if show_image or save_dir is not None:
            assert 'ori_image' in data
            img0 = data['ori_image'].numpy()[0]
            online_im = mot_vis.plot_tracking(
                img0,
                online_tlwhs,
                online_ids,
                online_scores,
                frame_id=frame_id,
                fps=1. / average_time)
        if show_image:
            cv2.imshow('online_im', online_im)
            
        if writer is not None:
            writer.write(online_im)
        elif save_dir is not None:
            cv2.imwrite(
                os.path.join(save_dir, '{:05d}.jpg'.format(frame_id)),
                online_im)
