

import glob
import numpy as np
import time
import tensorflow as tf
import json
from matplotlib import pyplot as plt
from object_detection.utils import ops as utils_ops
from object_detection.core import post_processing
from tqdm import tqdm
from PIL import Image, ImageDraw
from cocoapi.PythonAPI.pycocotools.coco import COCO
from cocoapi.PythonAPI.pycocotools.cocoeval import COCOeval
import copy
import os
utils_ops.tf = tf.compat.v1
# Patch the location of gfile
tf.gfile = tf.io.gfile

class nmsAnalysis:
    
    
    
    def __init__(self, models,imagesPath,annotationPath,catFocus=None, number_IoU_thresh=50,overall = False):
        assert(type(models) == list), print("Please input a list of model path")
        self.models = models
        self.imagesPath = imagesPath
        self.annotationPath = annotationPath
        self.resFilePath = self._createResFilePath()
        self.number_IoU_thresh = number_IoU_thresh
        self.iou_thresholdXaxis = np.linspace(0.2, 0.9, number_IoU_thresh)
        self.overall = overall
        self.coco = self.loadCocoApi()
        self.categories = self.getCategories() if catFocus is None else catFocus
        # All the variable that will change throughout the study and will be needed in many functions
        self._study = {
            "img": dict(),
            "catId": int(),
            "catStudied": str(),
            "all_output_dict": dict(),
            "modelPath": str(),
            "model": None,  # TF model
            "iouThreshold": float(),
        }
        
        self.graph_precision_to_recall = False
        self.with_train = False

    def _createResFilePath(self):

        return "cocoDt.json"

    def loadModel(self, modelPath):
        """Load associate tf OD model"""
        model_dir = modelPath + "/saved_model"
        detection_model = tf.saved_model.load(str(model_dir))
        detection_model = detection_model.signatures['serving_default']
        return detection_model

    def loadCocoApi(self):
        """Read annotations file and return the associate cocoApi"""
        annFile = self.annotationPath
        # initialize COCO api for instance annotations
        coco = COCO(annFile)
        return coco

    def getCategories(self):
        """Return list of categories one can study"""
        cats = self.coco.loadCats(self.coco.getCatIds())
        categories = [cat['name'] for cat in cats]
        return categories

    def getImgClass(self,category):
        # get all images containing given categories, and the Index of categories studied
        """
        input:
            catStudied: name among the category list
        output: 
            list img with dictionnary element of the form: 
            {'license': 5, 'file_name': '000000100274.jpg', 
            'coco_url': 'http://images.cocodataset.org/val2017/000000100274.jpg', 'height': 427, 
            'width': 640, 'date_captured': '2013-11-17 08:29:54', 
            'flickr_url': 'http://farm8.staticflickr.com/7460/9267226612_d9df1e1d14_z.jpg', 'id': 100274}

            catIds: List of index of categories stydied
        """
        catIds = self.coco.getCatIds(catNms=[category])
        imgIds = self.coco.getImgIds(catIds=catIds)
        img = self.coco.loadImgs(imgIds)

        self._study["img"] = img
        self._study["catId"] = catIds[0]
    
    def getCatId(self,category):
        "Return category Id in the dataset"
        catIds = self.coco.getCatIds(catNms=[category])
        return catIds[0]

    def load_all_output_dict(self):

        self._study["model"] = self.loadModel(self._study["modelPath"])
        filename = self._study["modelPath"] + "/all_output_dict.json"
        is_all_output_dict = os.path.isfile(filename)
        if not is_all_output_dict:
            print("Compute all the inferences boxes in the validation set for the model {} and save it for faster computations of you reuse the interface in {}/all_output_dict.json".format(
                self._study["modelPath"], self._study["modelPath"]))
            all_output_dict = self.computeInferenceBbox(self._study["model"])
            with open(filename, 'w') as fs:
                json.dump(all_output_dict, fs, indent=1)
        else:
            with open(filename, 'r') as fs:
                all_output_dict = json.load(fs)
        self._study["all_output_dict"] = all_output_dict

    def expand_image_to_4d(self, image):
        """image a numpy array representing a gray scale image"""
        # The function supports only grayscale images
        assert len(image.shape) == 2, "Not a grayscale input image"
        last_axis = -1
        dim_to_repeat = 2
        repeats = 3
        grscale_img_3dims = np.expand_dims(image, last_axis)
        training_image = np.repeat(
            grscale_img_3dims, repeats, dim_to_repeat).astype('uint8')
        assert len(training_image.shape) == 3
        assert training_image.shape[-1] == 3
        return training_image

    def run_inference_for_single_image(self, image):
        """
        input:
            model : OD model
            image : np.array format
        output:
            Dictionnary:
                key = ['num_detections','detection_classes','detection_boxes','detection_scores']
                valuesType = [int,list of int, list of 4 integers, list int]
        """
        image = np.asarray(image)
        # The input needs to be a tensor, convert it using `tf.convert_to_tensor`.
        input_tensor = tf.convert_to_tensor(image)
        # The model expects a batch of images, so add an axis with `tf.newaxis`.
        input_tensor = input_tensor[tf.newaxis, ...]

        # Run inference
        # If image doesn't respect the right format ignore it
        try:
            output_dict = self._study["model"](input_tensor)
        except:
            return None

        # All outputs are batches tensors.
        # Convert to numpy arrays, and take index [0] to remove the batch dimension.
        # We're only interested in the first num_detections.
        num_detections = int(output_dict.pop('num_detections'))
        output_dict = {key: value[0, :num_detections].numpy()
                       for key, value in output_dict.items()}

        key_of_interest = ['detection_scores',
                           'detection_classes', 'detection_boxes']
        output_dict = {key: list(output_dict[key]) for key in key_of_interest}
        output_dict["num_detections"] = int(num_detections)
        output_dict['detection_boxes'] = [[float(box) for box in output_dict['detection_boxes'][i]] for i in range(
            len(output_dict['detection_scores']))]
        output_dict['detection_scores'] = [
            float(box) for box in output_dict['detection_scores']]
        output_dict['detection_classes'] = [
            float(box) for box in output_dict['detection_classes']]

        if len(output_dict["detection_boxes"]) == 0:
            return None
        return output_dict

    def computeInferenceBbox(self):
        """
        For all the images in the coco img, compute the output_dict with
        run_inference_for_single_image. Store them as a dictionnary with keys
        being the index of the image in our coco dataset.

        input:
        - model: OD model

        output:
        A dictionnary describing the inferences for each image id associated to 
        a dictionary:
        {id: keyDic = ['num_detections','detection_classes','detection_boxes',
                    'detection_scores']}
        """
        all_output_dict = dict()
        i = 0
        folder = "/".join([self.imagesPath, "*.jpg"])
        for image_path in tqdm(glob.glob(folder)):
            # the array based representation of the image
            image = Image.open(image_path)
            image_np = np.array(image)
            """If image is gray_scale one need to reshape to dimension 4
            using the utility function defined above"""
            if len(image_np.shape) == 2:
                image_np = self.expand_image_to_4d(image_np)
            # Actual detection.
            output_dict = self.run_inference_for_single_image(image_np)
            if output_dict is None:
                continue
            idx = image_path.split("/")[-1]
            all_output_dict[idx] = output_dict
            i += 1
        return all_output_dict

    def computeNMS(self, output_dict):
        """
        input:
        -output_dict: the dictionnary ouput of the inference computation
        keyDic = ['num_detections','detection_classes','detection_boxes','detection_scores']
        - iou_threshold: it is used for the nms that will be applied after the OD

        output:
        A 3D tuple in this order:
        - final_classes : list of int64 telling the category of each detected bbox
        - final_scores : list of float64 scoring each bbox
        - final_boxes : list of coordinates of each bbox (format : [ymin,xmin,ymax,xmax] )
        """

        if not output_dict:
            return None, None, None

        # Apply the nms
        box_selection = tf.image.non_max_suppression_with_scores(
            output_dict['detection_boxes'], output_dict['detection_scores'], 100,
            iou_threshold=float(self._study["iouThreshold"]), score_threshold=float(
                '-inf'),
            soft_nms_sigma=0.0, name=None)

        # Index in the list output_dict['detection_boxes']
        final_boxes = list(box_selection[0].numpy())
        # Index in the list output_dict['detection_scores']
        final_scores = list(box_selection[1].numpy())
        final_classes = []
        for i in range(len(final_boxes)):
            index = final_boxes[i]
            # We want the actual bbox coordinate not the index
            final_boxes[i] = output_dict['detection_boxes'][index]
            final_classes.append(output_dict['detection_classes'][index])

        return final_classes, final_scores, final_boxes

    def putCOCOformat(self, boxes, im_width, im_height):
        """
        Transform a bbox in the OD format into cocoformat
        input:
            boxes: List of the form [ymin,xmin,ymax,xmax] in the percentage of the image scale
            im_width: real width of the associated image
            im_height: real height of the associated image
        output:
            List of the form [left,top,width,height] describing the bbox, in the image scale
        """
        # float to respect json format
        left = float(boxes[1]) * im_width
        right = float(boxes[3]) * im_width
        top = float(boxes[0]) * im_height
        bottom = float(boxes[2]) * im_height
        width = right - left
        height = bottom - top

        return [left, top, width, height]

    def writeResJson(self, newFile=True):
        """
        Write a Json file in the coco annotations format for bbox detections

        input:
            img: coco class describing the images to study
            resFilePath: path of the file where the result annotations will be written
            all_output_dict: dict with key: image Id in the coco dataset and value the output_dict
            computed with the OD.
            iou_threshold: Paramaeter for the nms algorithm that will be applied to the OD
        output:
            Json file of the form:
            [{"image_id":42,"category_id":18,"bbox":[258.15,41.29,348.26,243.78],"score":0.236}...]
            The length of this list is lengthData

            imgIds: List of the image ids that are studied
        """
        result = []
        imgIds = set()  # set to avoid repetition
        key_of_interest = ['detection_scores',
                           'detection_classes', 'detection_boxes']
        for img in self._study["img"]:
            imgId = img["id"]
            imgIds.add(imgId)

            output_dict = copy.deepcopy(
                self._study["all_output_dict"][img['file_name']])
            if output_dict == None:
                continue

            # remove detection of other classes that are not studied
            idx_to_remove = [i for i, x in enumerate(
                output_dict["detection_classes"]) if x != self._study["catId"]]
            for index in sorted(idx_to_remove, reverse=True):
                del output_dict['detection_scores'][index]
                del output_dict['detection_classes'][index]
                del output_dict['detection_boxes'][index]
                output_dict["num_detections"] -= 1
            num_detections = output_dict["num_detections"]
            output_dict = {key: np.array(
                output_dict[key]) for key in key_of_interest}
            output_dict["num_detections"] = num_detections
            if len(output_dict['detection_boxes']) == 0:
                output_dict = None

            final_classes, final_scores, final_boxes = self.computeNMS(
                output_dict)

            if not final_classes:
                continue

            for j in range(len(final_classes)):

                #ex : {"image_id":42,"category_id":18,"bbox":[258.15,41.29,348.26,243.78],"score":0.236}
                properties = {}
                # json format doesnt support int64
                properties["category_id"] = int(final_classes[j])
                properties["image_id"] = imgId
                im_width = img['width']
                im_height = img['height']
                # we want [ymin,xmin,ymax,xmax] -> [xmin,ymin,width,height]
                properties["bbox"] = self.putCOCOformat(
                    final_boxes[j], im_width, im_height)
                properties["score"] = float(final_scores[j])

                result.append(properties)
        if newFile:
            with open(self.resFilePath, 'w') as fs:
                json.dump(result, fs, indent=1)
        else:
            with open(self.resFilePath, 'r') as fs:
                data = json.load(fs)
                result += data
            with open(self.resFilePath, 'w') as fs:
                json.dump(result, fs, indent=1)

        return list(imgIds)

    def getClassAP(self):
        """
        Return list of AP score evaluated on the category studied  for every un iou in np.linspace(0.2,0.9,self.number_IoU_thresh)
        """

        AP = []
        FN = []
        computeInstances = True
        for iouThreshold in tqdm(self.iou_thresholdXaxis, desc="progressbar IoU Threshold"):

            self._study["iouThreshold"] = iouThreshold
            # Create the Json result file and read it.
            imgIds = self.writeResJson()
            try:
                cocoDt = self.coco.loadRes(self.resFilePath)
            except:
                return 1
            cocoEval = COCOeval(self.coco, cocoDt, 'bbox')
            cocoEval.params.imgIds = imgIds
            cocoEval.params.catIds = self._study["catId"]
            # Here we increase the maxDet to 1000 (same as in model config file)
            # Because we want to optimize the nms that is normally in charge of dealing with
            # bbox that detects the same object twice or detection that are not very precise
            # compared to the best one.
            cocoEval.params.maxDets = [1, 10, 1000]
            cocoEval.evaluate()
            number_FN = 0
            if computeInstances:
                instances_non_ignored = 0
            for evalImg in cocoEval.evalImgs:
                number_FN += sum(evalImg["FN"])
                if computeInstances:
                    instances_non_ignored += sum(
                        np.logical_not(evalImg['gtIgnore']))
            computeInstances = False
            FN.append(int(number_FN))
            cocoEval.accumulate(
                iouThreshold, withTrain=self.with_train, category=self._study["catStudied"])

            cocoEval.summarize()
            # readDoc and find self.evals
            AP.append(cocoEval.stats[1])
            precisions = cocoEval.s.reshape((101,))
            if self.graph_precision_to_recall:
                self.precisionToRecall(precisions)
    
        general_folder = "{}/nms_analysis".format(self._study["modelPath"])
        if not os.path.isdir(general_folder):
            os.mkdir(general_folder)
        general_folder += "/AP[IoU=0.5]/"
        if not os.path.isdir(general_folder):
            os.mkdir(general_folder)
            
        if not self.with_train:
            general_folder += "validation/"
        else:
            general_folder += "validation_train/"
        if not os.path.isdir(general_folder):
            os.mkdir(general_folder)

        with open(general_folder + "{}.json".format(self._study["catStudied"]), 'w') as fs:
            json.dump({"iou threshold": list(self.iou_thresholdXaxis), "AP[IoU:0.5]": AP, "False Negatives": FN,
                       "number of instances": int(instances_non_ignored)}, fs, indent=1)

        return 0

    def getOverallAP(self):
        """
        Return list of AP score evaluated on the entire list self.categories for every un iou in np.linspace(0.2,0.9,self.number_IoU_thresh)
        """

        AP = []
        FN = []
        computeInstances = True

        for iouThreshold in tqdm(self.iou_thresholdXaxis, desc="progressbar IoU Threshold"):
            self._study["iouThreshold"] = iouThreshold
            allCatIds = []
            allImgIds = []
            for i, category in tqdm(enumerate(self.categories), desc="category"):
                    
                self.getImgClass(category)
                allCatIds += [self._study["catId"]]
            # Create the Json result file and read it.
                if i == 0:
                    imgIds = self.writeResJson(newFile=True)

                else:
                    imgIds = self.writeResJson(newFile=False)
                allImgIds += imgIds
            try:
                cocoDt = self.coco.loadRes(self.resFilePath)
            except:
                return 1
            cocoEval = COCOeval(self.coco, cocoDt, 'bbox')
            cocoEval.params.imgIds = allImgIds
            cocoEval.params.catIds = allCatIds
            # Here we increase the maxDet to 1000 (same as in model config file)
            # Because we want to optimize the nms that is normally in charge of dealing with
            # bbox that detects the same object twice or detection that are not very precise
            # compared to the best one.
            cocoEval.params.maxDets = [1, 10, 1000]
            cocoEval.evaluate()
            number_FN = 0
            if computeInstances:
                instances_non_ignored = 0

            for evalImg in cocoEval.evalImgs:
                if evalImg != None:
                    number_FN += sum(evalImg["FN"])
                    if computeInstances:
                        instances_non_ignored += sum(
                            np.logical_not(evalImg['gtIgnore']))
            computeInstances = False
            FN.append(int(number_FN))
            cocoEval.accumulate(iouThreshold, withTrain=False, category='all')

            cocoEval.summarize()
            # readDoc and find self.evals
            AP.append(cocoEval.stats[1])

        general_folder = "{}/nms_analysis/AP[IoU=0.5]/".format(self._study["modelPath"])
        if not os.path.isdir(general_folder):
            os.mkdir(general_folder)

        if not self.with_train:
            general_folder += "validation/"
        else:
            general_folder += "validation_train/"
        if not os.path.isdir(general_folder):
            os.mkdir(general_folder)
        with open(general_folder + "all.json", 'w') as fs:
            json.dump({"iou threshold": list(self.iou_thresholdXaxis), "AP[IoU:0.5]": AP, "False Negatives": FN,
                       "number of instances": int(instances_non_ignored)}, fs, indent=1)

        return 0

    def precisionToRecall(self, precision):
        """
        precision -[all] P = 101. Precision for each recall
        catStudied: String describing the category of image studied
        modelPath: path to the model to save the graph
        """
        plt.figure(figsize=(18, 10))
        recall = np.linspace(0, 1, 101)
        iouThreshold = round(self._study["iouThreshold"], 3)

        # Plot the data
        plt.plot(recall, precision,
                 label='Precision to recall for IoU = {}'.format(iouThreshold))
        # Add a legend
        plt.legend(loc="lower left")
        plt.title('Class = {}'.format(self._study["catStudied"]))
        plt.xlabel('Recall')
        plt.ylabel('Precision')

        # Create correct folder
        general_folder = "{}/nms_analysis/precision_to_recall/".format(
            self._study["modelPath"])
        if not os.path.isdir(general_folder):
            os.mkdir(general_folder)

        if self.with_train:
            general_folder += "validation_train/"
        else:
            general_folder += "validation/"

        if not os.path.isdir(general_folder):
            os.mkdir(general_folder)

        category_folder = general_folder + \
            self._study["catStudied"].replace(' ', '_')
        if not os.path.isdir(category_folder):
            os.mkdir(category_folder)


        plt.savefig(category_folder +
                    '/iou={}.png'.format(iouThreshold), bbox_inches='tight')

        # plt.clf()
        plt.close('all')

    def runAnalysis(self):
        
        if self.with_train:
            if not os.path.isdir('FN_with_nms/'):
                print("Please run analysis on the groundtruth in order to know the number of false negatives genreated by nms.")
                return
        for modelPath in self.models:
            self._study["modelPath"] = modelPath
            self.load_all_output_dict()
            for catStudied in tqdm(self.categories, desc="Categories Processed", leave=False):
                self._study["catStudied"] = catStudied
                self.getImgClass(catStudied)
                self.getClassAP()
            if self.overall and not self.with_train:
                self.getOverallAP()
                
        os.remove(self.resFilePath)