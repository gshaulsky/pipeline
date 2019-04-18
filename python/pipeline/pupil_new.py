import os
# Disable DLC GUI first, then import deeplabcut
os.environ["DLClight"] = "True"

from .exceptions import PipelineException
from . import experiment, notify
from .utils import h5
from . import config
from .utils.decorators import gitlog
from .utils.DLC_tools import PupilFitting

from commons import lab

import numpy as np
import cv2
import pandas as pd
from tqdm import tqdm

from datajoint.jobs import key_hash
from datajoint.autopopulate import AutoPopulate
import datajoint as dj

import deeplabcut as dlc
from deeplabcut.utils import auxiliaryfunctions

gputouse = 0

schema = dj.schema('pipeline_eye_DLC', locals())

pipeline_eye = dj.create_virtual_module('pipeline_eye', 'pipeline_eye')
pipeline_experiment = dj.create_virtual_module('pipeline_experiment', 'pipeline_experiment')

# If config.yaml ever updated, make sure you store the file name differently so that it becomes unique
@schema
class ConfigDeeplabcut(dj.Manual):
    definition = """
    # Minimal info needed to load deeplabcut model
    config_path                             : varchar(255)          # path to deeplabcut config
    ---
    shuffle                                 : smallint unsigned     # shuffle number used for the trained dlc model. Needed for dlc.analyze_videos
    trainingsetindex                        : smallint unsigned     # trainingset index used for the trained dlc. model. Needed for dlc.analyze_videos
    """


@schema
class TrackedLabelsDeeplabcut(dj.Computed):
    definition = """
    # Tracking table using deeplabcut
    -> pipeline_eye.Eye
    -> pipeline_experiment.Scan
    -> ConfigDlc
    ---
    tracking_ts=CURRENT_TIMESTAMP           : timestamp             # automatic
    tracking_dir                            : varchar(255)          # path to tracking directory
    """

    class OriginalVideo(dj.Part):
        definition = """
        # original video information
        -> master
        ---
        original_width                     : smallint unsigned      # original video width size
        original_height                    : smallint unsigned      # original video height size
        video_path                         : varchar(255)           # path to original video
        """

    class ShortVideo(dj.Part):
        definition = """
        # 5 seconds long video starting from the middle frame of the original video
        -> master
        ---
        starting_frame                     : int unsigned           # middle frame of the original video
        video_path                         : varchar(255)           # path to short video
        """

    class CompressedCroppedVideo(dj.Part):
        definition = """
        # Compressed and cropped video information
        -> master
        ---
        cropped_x0                         : smallint unsigned      # start width coord wrt original video
        cropped_x1                         : smallint unsigned      # end width coord wrt original video
        cropped_y0                         : smallint unsigned      # start height coord wrt original video
        cropped_y1                         : smallint unsigned      # end height coord wrt original video
        added_pixels                       : smallint unsigned      # number of pixels added around the cropping coords
        video_path                         : varchar(255)           # path to comparessed & cropped video
        """

    @property
    def key_source(self):

        new_ConfigDeeplabcut = ConfigDeeplabcut & {
            'config_path': '/mnt/scratch07/donnie/DeepLabCut/pupil_track-Donnie-2019-02-12/config.yaml'}

        new_key_source = (pipeline_eye.Eye * pipeline_experiment.Scan * new_ConfigDeeplabcut).proj() & {
            'animal_id': 20892, 'scan_idx': 10, 'session': 9}

        return new_key_source

    def get_video_path(self, key):
        """
        Input:
            key: dictionary
                A key that consists of animal_id, session, and scan_idx
        """
        video_info = (pipeline_experiment.Session() *
                      pipeline_experiment.Scan.EyeVideo() & key).fetch1()
        video_path = lab.Paths().get_local_path(
            "{behavior_path}/{filename}".format(**video_info))
        return video_path

    def create_tracking_directory(self, key):
        """
        this function creates the following directory structure:

        video_original_dir
            |
            |------ video_original
            |------ tracking_dir (create_tracking_folder)
                        |------- symlink to video_original (add_symlink) 
                        |------- cropped_dir
                                    |------- cropped_video (generated from make_short_video)
                                    |------- h5 file for cropped video (generated by deeplabcut)
                                    |------- pickle for cropped video (generated by deeplabcut)
                        |------- short_dir
                                    |------- short_video (generated by make_short_video function)
                                    |------- h5 file for short video(generated by deeplabcut)
                                    |------- pickle for short video (generated by deeplabcut)

        Input:
            key: dictionary
                a dictionary that contains mouse id, session, and scan idx.

        Return:
            tracking_dir: string
                a string that specifies the path to the tracking directory
        """

        print("Generating tracking directory for ", key)

        vid_path = self.get_video_path(key)
        vid_dir = os.path.dirname(os.path.normpath(vid_path))
        tracking_dir_name = os.path.basename(
            os.path.normpath(vid_path)).split('.')[0] + '_tracking'

        tracking_dir = os.path.join(vid_dir, tracking_dir_name)

        symlink_path = os.path.join(
            tracking_dir, os.path.basename(os.path.normpath(vid_path)))

        if not os.path.exists(tracking_dir):

            os.mkdir(tracking_dir)
            os.mkdir(os.path.join(tracking_dir, 'compressed_cropped'))
            os.mkdir(os.path.join(tracking_dir, 'short'))

            os.symlink(vid_path, symlink_path)

        else:
            print('{} already exists!'.format(tracking_dir))

        return tracking_dir, symlink_path

    def make_short_video(self, tracking_dir):
        """
        Extract 5 seconds long video starting from the middle of the original video.

        Input:
            tracking_dir: string
                String that specifies the full path of tracking directory
        Return:
            None
        """
        from subprocess import Popen, PIPE

        suffix = '_short.avi'

        case = os.path.basename(os.path.normpath(
            tracking_dir)).split('_tracking')[0]

        input_video_path = os.path.join(tracking_dir, case + '.avi')

        out_vid_path = os.path.join(tracking_dir, 'short', case + suffix)

        cap = cv2.VideoCapture(input_video_path)

        original_width = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        original_height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)

        fps = cap.get(cv2.CAP_PROP_FPS)
        mid_frame_num = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)/2)
        duration = int(mid_frame_num/fps)

        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)

        print('\nMaking a short video!')

        cmd = ['ffmpeg', '-i', input_video_path, '-ss',
               '{}:{}:{}'.format(hours, minutes, seconds), '-t', '5', '-c', 'copy', out_vid_path]

        # call ffmpeg to make a short video
        p = Popen(cmd, stdin=PIPE)
        # close ffmpeg
        p.wait()

        print('\nSuccessfully created a short video!')

        return out_vid_path, original_width, original_height, mid_frame_num

    def predict_labels(self, vid_path, config):
        destfolder = os.path.dirname(vid_path)
        dlc.analyze_videos(config=config['config_path'], videos=[vid_path], videotype='avi', shuffle=config['shuffle'],
                           trainingsetindex=config['trainingsetindex'], gputouse=gputouse, save_as_csv=False, destfolder=destfolder)

    def obtain_cropping_coords(self, short_h5_path, DLCscorer, config):
        """
        First, filter out by the pcutoff, then find values that are within 1 std from mean 
        for each eyelid bodypart. Then, compare among the parts and find min,max values in x and y.

        The reason we use 1 std from mean is that dlc might have outliers in this short video.
        Hence we filter out these potential outliers
        """

        # there should be only 1 h5 file generated by dlc
        df_short = pd.read_hdf(short_h5_path)

        eyelid_cols = ['eyelid_top', 'eyelid_right',
                       'eyelid_left', 'eyelid_bottom']

        df_eyelid = df_short[DLCscorer][eyelid_cols]

        df_eyelid_likelihood = df_eyelid.iloc[:, df_eyelid.columns.get_level_values(
            1) == 'likelihood']
        df_eyelid_x = df_eyelid.iloc[:, df_eyelid.columns.get_level_values(
            1) == 'x']
        df_eyelid_y = df_eyelid.iloc[:, df_eyelid.columns.get_level_values(
            1) == 'y']

        df_eyelid_coord = dict(x=df_eyelid_x, y=df_eyelid_y)

        coords_dict = dict(xmin=[], xmax=[], ymin=[], ymax=[])

        for eyelid_label in eyelid_cols:

            for coord in ['x', 'y']:

                eyelid_coord_pcutoff = df_eyelid_coord[coord][(
                    df_eyelid_likelihood.loc[:, eyelid_label].values > config['pcutoff'])][eyelid_label][coord].values

                eyelid_coord_68 = eyelid_coord_pcutoff[(eyelid_coord_pcutoff < np.mean(eyelid_coord_pcutoff) + np.std(eyelid_coord_pcutoff)) *
                                                       (eyelid_coord_pcutoff > np.mean(
                                                           eyelid_coord_pcutoff) - np.std(eyelid_coord_pcutoff))]

                coords_dict[coord+'min'].append(eyelid_coord_68.min())
                coords_dict[coord+'max'].append(eyelid_coord_68.max())

        cropped_coords = {}
        cropped_coords['cropped_x0'] = int(min(coords_dict['xmin']))
        cropped_coords['cropped_x1'] = int(max(coords_dict['xmax']))
        cropped_coords['cropped_y0'] = int(min(coords_dict['ymin']))
        cropped_coords['cropped_y1'] = int(max(coords_dict['ymax']))

        return cropped_coords

    def add_pixels(self, cropped_coords, original_width, original_height, pixel_num):

        if cropped_coords['cropped_x0'] - pixel_num < 0:
            cropped_coords['cropped_x0'] = 0
        else:
            cropped_coords['cropped_x0'] -= pixel_num

        if cropped_coords['cropped_x1'] + pixel_num > original_width:
            cropped_coords['cropped_x1'] = original_width
        else:
            cropped_coords['cropped_x1'] += pixel_num

        if cropped_coords['cropped_y0'] - pixel_num < 0:
            cropped_coords['cropped_y0'] = 0
        else:
            cropped_coords['cropped_y0'] -= pixel_num

        if cropped_coords['cropped_y1'] + pixel_num > original_height:
            cropped_coords['cropped_y1'] = original_height
        else:
            cropped_coords['cropped_y1'] += pixel_num

        return cropped_coords

    def make_compressed_cropped_video(self, tracking_dir, cropped_coords):
        from subprocess import Popen, PIPE

        suffix = '_compressed_cropped.avi'

        case = os.path.basename(os.path.normpath(
            tracking_dir)).split('_tracking')[0]

        input_video_path = os.path.join(tracking_dir, case + '.avi')

        out_vid_path = os.path.join(
            tracking_dir, 'compressed_cropped', case + suffix)

        out_w = cropped_coords['cropped_x1'] - cropped_coords['cropped_x0']
        out_h = cropped_coords['cropped_y1'] - cropped_coords['cropped_y0']
        print('\nMaking a compressed and cropped video!')

        # crf: use value btw 17 and 28 (lower the number, higher the quality of the video)
        # intra: no compressing over time. only over space
        cmd = ['ffmpeg', '-i', '{}'.format(input_video_path), '-vcodec', 'libx264', '-crf', '17', '-intra', '-filter:v',
               "crop={}:{}:{}:{}".format(out_w, out_h, cropped_coords['cropped_x0'], cropped_coords['cropped_y0']), '{}'.format(out_vid_path)]

        # call ffmpeg to make a short video
        p = Popen(cmd, stdin=PIPE)
        # close ffmpeg
        p.wait()
        print('\nSuccessfully created a compressed & cropped video!\n')

        return out_vid_path

    def make(self, key):

        print('Tracking labels with DLC')

        temp_config = (ConfigDeeplabcut & key).fetch1()
        config = auxiliaryfunctions.read_config(temp_config['config_path'])
        config['config_path'] = temp_config['config_path']
        config['shuffle'] = temp_config['shuffle']
        config['trainingsetindex'] = temp_config['trainingsetindex']

        trainFraction = config['TrainingFraction'][config['trainingsetindex']]
        DLCscorer = auxiliaryfunctions.GetScorerName(
            config, config['shuffle'], trainFraction)

        # make needed directories
        tracking_dir, original_video_path = self.create_tracking_directory(key)
        self.insert1(dict(key, tracking_dir=tracking_dir))

        # make a short video (5 seconds long)
        short_video_path, original_width, original_height, mid_frame_num = self.make_short_video(
            tracking_dir)

        # save info about original video
        original_video = self.OriginalVideo()
        original_video.insert1(
            dict(key, original_width=original_width,
                 original_height=original_height,
                 video_path=original_video_path))

        # save info about short video
        short_video = self.ShortVideo()
        short_video.insert1(
            dict(key, starting_frame=mid_frame_num, video_path=short_video_path))

        short_h5_path = short_video_path.split('.')[0] + DLCscorer + '.h5'

        # predict using the short video
        self.predict_labels(short_video_path, config)

        # obtain the cropping coordinates from the prediciton on short video
        cropped_coords = self.obtain_cropping_coords(
            short_h5_path, DLCscorer, config)

        # add 100 pixels around cropping coords. Ensure that it is within the original dim
        pixel_num = 100
        cropped_coords = self.add_pixels(cropped_coords=cropped_coords,
                                         original_width=original_width,
                                         original_height=original_height,
                                         pixel_num=pixel_num)

        # make a compressed and cropped video
        compressed_cropped_video_path = self.make_compressed_cropped_video(
            tracking_dir, cropped_coords)

        # predict using the compressed and cropped video
        self.predict_labels(compressed_cropped_video_path, config)

        compressed_cropped_video = self.CompressedCroppedVideo()
        compressed_cropped_video.insert1(dict(key, cropped_x0=cropped_coords['cropped_x0'],
                                              cropped_x1=cropped_coords['cropped_x1'],
                                              cropped_y0=cropped_coords['cropped_y0'],
                                              cropped_y1=cropped_coords['cropped_y1'],
                                              added_pixels=pixel_num,
                                              video_path=compressed_cropped_video_path))


@schema
class FittedContourDeeplabcut(dj.Computed):
    definition = """
    # Fit a circle and an ellipse using compressed & cropped video. 
    -> TrackedLabelsDeeplabcut   
    ---
    fitting_ts=CURRENT_TIMESTAMP    : timestamp  # automatic
    """

    class Circle(dj.Part):
        definition = """
        -> master
        frame_id                 : int           # frame id with matlab based 1 indexing
        ---
        center=NULL              : tinyblob      # center of the circle in (x, y) of image
        radius=NULL              : float         # radius of the circle
        visible_portion=NULL     : float         # portion of visible pupil area given a fitted circle frame. Please refer DLC_tools.PupilFitting.detect_visible_pupil_area for more details
        """

    class Ellipse(dj.Part):
        definition = """
        -> master
        frame_id                 : int           # frame id with matlab based 1 indexing
        ---
        center=NULL              : tinyblob      # center of the ellipse in (x, y) of image
        major_radius=NULL        : float         # major radius of the ellipse
        minor_radius=NULL        : float         # minor radius of the ellipse
        rotation_angle=NULL      : float         # ellipse rotation angle in degrees w.r.t. major_radius
        visible_portion=NULL     : float         # portion of visible pupil area given a fitted ellipse frame. Please refer DLC_tools.PupilFitting.detect_visible_pupil_area for more details
        """

    def make(self, key):
        print("Fitting:", key)

        shuffle, trainingsetindex = (ConfigDeeplabcut & key).fetch1(
            'shuffle', 'trainingsetindex')
        cc_info = (TrackedLabelsDeeplabcut.CompressedCroppedVideo() & key).fetch1()

        config = auxiliaryfunctions.read_config(cc_info['config_path'])
        config['config_path'] = cc_info['config_path']
        config['shuffle'] = shuffle
        config['trainingsetindex'] = trainingsetindex
        config['video_path'] = cc_info['video_path']

        pupil_fit = PupilFitting(config=config, bodyparts='all')

        self.insert1(key)

        for frame_num in tqdm(range(pupil_fit.clip.nframes)):

            fit_dict = pupil_fit.fitted_core(frame_num=frame_num)

            circle = FittedContourDeeplabcut.Circle()
            circle.insert1(dict(key, frame_id=frame_num,
                                center=fit_dict['circle_fit']['center'],
                                radius=fit_dict['circle_fit']['radius'],
                                visible_portion=fit_dict['circle_visible']['visible_portion']))

            ellipse = FittedContourDeeplabcut.Ellipse()
            ellipse.insert1(dict(key, frame_id=frame_num,
                                 center=fit_dict['ellipse_fit']['center'],
                                 major_radius=fit_dict['ellipse_fit']['major_radius'],
                                 minor_radius=fit_dict['ellipse_fit']['minor_radius'],
                                 rotation_angle=fit_dict['ellipse_fit']['rotation_angle'],
                                 visible_portion=fit_dict['ellipse_visible']['visible_portion']))
