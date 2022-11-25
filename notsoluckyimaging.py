#!/usr/bin/python3
import time, os
import numpy as np
from datetime import datetime
from multiprocessing import Process, Manager
import matplotlib.pyplot as plt
import __main__ as main

#camera functions
from picamera2 import Picamera2
from picamera2.encoders import Encoder

### image functions
from pidng.core import RAW2DNG, DNGTags, Tag
from pidng.defs import *
from PIL import Image
from scipy.ndimage import laplace
from skimage.util.shape import view_as_windows
from skimage.measure import blur_effect
import skimage
from astropy.io import fits

session = {
    "object_name": "Jupiter",
    "save_image_dng" : False,
    "save_image_fits" : True,
    "img_folder": "/home/pi/luckyimaging/",
    "tmp_folder": "/home/pi/ramdisk/",
    "num_cpus": 4
}

raw_camera_settings = {
    "size": (1920, 1080),
    "bayerformat": "SRGGB12",
    "bit_depth": 12,
    "max_fps": 60,
    "analoggain": (1, 31) # (min, max)
}

manual_quality_settings = {
    "analyse_frames_per_settings": 10,
    "min_dynamic_range": 20, # %
    "percentile": (0.1, 99.99),
    #higher is better, no maximum
    "laplace_threshold": 7.0,
    # lower is better, range from 0 to 1.0
    "blur_threshold": 0.90,
    "use_gains": [1, 10],
    "use_exposures": [20e3, 40e3, 200e3]
}

config = {
    "AeEnable": False, 
    "AwbEnable": False, 
    "FrameRate": raw_camera_settings["max_fps"],
    "ExposureTime": int(1e6 // raw_camera_settings["max_fps"]),
    "AnalogueGain": 1.0,
    #"ScalerCrop": offset + size # roi could be used later
}

max_adc_range = 2**raw_camera_settings["bit_depth"] - 1
pixel_cnt = raw_camera_settings["size"][0] * raw_camera_settings["size"][1]


def stride_conv_strided(arr):

    # convolution to debayer to grayscale with 1/4 resolution
    arr2 = np.array([[0.2989, 0.5870/2],
                    [0.5870/2, 0.1140]])

    s = 2

    def strided4D(arr, arr2, s):
        return view_as_windows(arr, arr2.shape, step=s)

    arr4D = strided4D(arr,arr2,s=s)
    return np.tensordot(arr4D, arr2, axes=((2,3),(0,1)))


def process_raw_video(file_path, results):
    framelist = []
    # read requested frames in the video
    try:
        with open(file_path, "rb") as file_p:
            for _ in range(manual_quality_settings["analyse_frames_per_settings"] + 1):
                buf = file_p.read(pixel_cnt * 2)
                framelist.append(np.frombuffer(buf, dtype=np.uint16).reshape((raw_camera_settings["size"][1], raw_camera_settings["size"][0])))
    except Exception as e:
        return

    print("{:d} frames read - start processing".format(len(framelist)))

    for i, arr in enumerate(framelist):
        # discard the first frame - possible other settings
        if i == 0: 
            continue
        
        if i > manual_quality_settings["analyse_frames_per_settings"]:
            break

        min_value, max_value = np.percentile(arr, manual_quality_settings["percentile"])

        range_percent = (max_value - min_value) / max_adc_range * 100

        if range_percent < manual_quality_settings["min_dynamic_range"]:
            print("Low dynamic range (min {:.1f}, max {:.1f}) detected - skip". format(min_value, max_value))
            continue

        if max_value >= max_adc_range:
            print("Sensor saturation - skip")
            continue
        
        # convert bayer to grayscale with convolution - there is no fancy debayer algo. ongoing
        gray_image = stride_conv_strided(arr)

        # first blure test option - laplace kernel
        laplaceout = np.std(laplace(gray_image))

        # second blure test option - skikit image
        blur_metric = blur_effect(gray_image)

        image_passed = True
        if laplaceout < manual_quality_settings["laplace_threshold"]:
            image_passed = False

        if blur_metric > manual_quality_settings["blur_threshold"]:
            image_passed = False

        print("Min: {:.0f} Max: {:.0f}, Laplace: {:.1f} Blur: {:.3f} {}".format(min_value, max_value , 
                    laplaceout, blur_metric, "*" if image_passed else ""))

        if image_passed:

            out = {
                "config" : config,
                "min" : min_value,
                "max" : max_value,
                "gray" : gray_image,
                "raw" : arr,
                "laplace" : laplaceout,
                "blurmetric" : blur_metric,
                "pass" : image_passed,
                "ts" : datetime.now()
            }

            results.append(out)

    # delete raw file to free memory
    os.remove(file_path)


def writeDNGFile(res_dict):
    # we take this from capture_video_raw example
    # Create DNG file from frame, based on https://github.com/schoolpost/PiDNG/blob/master/examples/raw2dng.py
    r = RAW2DNG()
    t = DNGTags()
    t.set(Tag.ImageWidth, raw_camera_settings["size"][0])
    t.set(Tag.ImageLength, raw_camera_settings["size"][1])
    t.set(Tag.TileWidth, raw_camera_settings["size"][0])
    t.set(Tag.TileLength, raw_camera_settings["size"][1])
    t.set(Tag.Orientation, Orientation.Horizontal)
    t.set(Tag.PhotometricInterpretation, PhotometricInterpretation.Color_Filter_Array)
    t.set(Tag.SamplesPerPixel, 1)
    t.set(Tag.BitsPerSample, raw_camera_settings["bit_depth"])
    t.set(Tag.CFARepeatPatternDim, [2, 2])
    t.set(Tag.CFAPattern, CFAPattern.RGGB)
    t.set(Tag.DNGVersion, DNGVersion.V1_4)
    t.set(Tag.DNGBackwardVersion, DNGVersion.V1_2)
    t.set(Tag.Model, "IMX290C")
    r.options(t, path="", compress=False)
    filename_dng = "{:.3}_{}_G{:02d}_E{:04d}_{:f}".format(
        res_dict["blurmetric"], session["object_name"], res_dict["config"]["AnalogueGain"], 
        res_dict["config"]["ExposureTime"] // 1000, datetime.timestamp(res_dict["ts"]))
    r.convert(res_dict["raw"], filename=os.path.join(session["img_folder"], filename_dng ) )
    
    print(filename_dng + " saved!")

def writeFITSFile(res_dict):
    filename_fits = "{:.3}_{}_G{:02d}_E{:04d}_{:f}.fits".format(
        res_dict["blurmetric"], session["object_name"], res_dict["config"]["AnalogueGain"], 
        res_dict["config"]["ExposureTime"] // 1000, datetime.timestamp(res_dict["ts"]))

    hdu = fits.PrimaryHDU(data=res_dict["raw"])
    hdu.header["BSCALE"] = 1
    hdu.header["BITPIX"] = raw_camera_settings["bit_depth"]
    hdu.header["BZERO"] = 2**raw_camera_settings["bit_depth"]
    hdu.header["DATE-OBS"] = str(res_dict["ts"].isoformat())
    hdu.header["OBJECT"] = session["object_name"]
    hdu.header["OBSERVER"] = main.__file__
    hdu.header["IMAGETYP"] = "LIGHT"
    hdu.header["EXPOSURE"] = res_dict["config"]["ExposureTime"] // 1e6
    hdu.header["EXPTIME"] = hdu.header["EXPOSURE"]
    hdu.header["INSTRUME"] = "IMX290C"
    hdu.header["GAIN"] = res_dict["config"]["AnalogueGain"]
    hdu.header["XPIXSZ"] = raw_camera_settings["size"][0]
    hdu.header["YPIXSZ"] = raw_camera_settings["size"][1]
    hdu.header["BAYERPAT"] = "RGGB"
    hdu.header["XBAYROFF"] = 0
    hdu.header["YBAYROFF"] = 0
    #print(hdu.header)
    hdu.writeto(os.path.join(session["img_folder"], filename_fits ))

    print(filename_fits + " saved!")

if __name__ == "__main__":
    # create a thread save list for the results
    manager = Manager()
    results = manager.list([])

    # init camera device
    picam2 = Picamera2()
    video_config = picam2.create_video_configuration(raw={"format": raw_camera_settings["bayerformat"], 'size': raw_camera_settings["size"]})
    picam2.configure(video_config)
    picam2.encode_stream_name = "raw"
    encoder = Encoder()

    # create process slots 
    procs = [None] * session["num_cpus"]

    for gain in manual_quality_settings["use_gains"]:
        for exposure in manual_quality_settings["use_exposures"]:

            # fps req. for the given expsoure
            fps_exposure = 1e6 / exposure

            # do camera settings
            config["AnalogueGain"] = gain
            config["ExposureTime"] = int(exposure)
            config["FrameRate"] = min(fps_exposure, 30)
            picam2.set_controls(config)

            print("Stard recording with Gain: {} and Exposure time: {} ms".format(config["AnalogueGain"], config["ExposureTime"] / 1e3))

            # record into a ramfs or tmpfs storage for speed
            tmp_video_path = os.path.join(session["tmp_folder"], "tmp" + str(datetime.timestamp(datetime.now())) + ".raw")
            tmp_pts_path = os.path.join(session["tmp_folder"], "timestamp.txt")
            picam2.start_recording(encoder, tmp_video_path, pts=tmp_pts_path)
            # for small exposures record min two seconds to grab enouth frames
            time.sleep(max(2, (manual_quality_settings["analyse_frames_per_settings"] + 2) * 1/fps_exposure))

            picam2.stop_recording()

            # search free proccess slot
            keep_while_running = True
            while keep_while_running:
                for i, proc in enumerate(procs):
                    if proc is None or proc.is_alive() == False:
                        procs[i] = Process(target=process_raw_video, args=(tmp_video_path, results))
                        procs[i].start()
                        keep_while_running = False
                        break
                time.sleep(0.1)

    #wait for all ongoing threads
    for proc in procs:
        if proc is not None:
            proc.join()

    if len(results) <= 0:
        print("No image data recorded")
        exit()            

    # take the worst and the best to display the gray scale images
    bestseeing = min(results, key=lambda x:x['blurmetric'])
    worstseeing = max(results, key=lambda x:x['blurmetric'])

    hist_best, _ = np.histogram(bestseeing["gray"], 64, (0, max_adc_range))
    hist_worst, _ = np.histogram(worstseeing["gray"], 64, (0, max_adc_range))

    ### display grayscale images
    f, axarr = plt.subplots(2,2)
    best_im = bestseeing["gray"]
    best_im = skimage.exposure.rescale_intensity(best_im, out_range=(0, 255))
    print("Best blure", bestseeing['blurmetric'])
    axarr[0,0].imshow(best_im, cmap='gray', vmin=0, vmax=255)
    worst_img = worstseeing["gray"]
    worst_img = skimage.exposure.rescale_intensity(worst_img, out_range=(0, 255))
    print("Worst blure", worstseeing['blurmetric'])
    axarr[1,0].imshow(worst_img, cmap='gray', vmin=0, vmax=255)

    ### display historgrams
    x_histo = np.linspace(0, max_adc_range, len(hist_best), endpoint = False)
    axarr[0,1].plot(x_histo, hist_best)
    #axarr[0,1].set_yscale("log")
    axarr[1,1].plot(x_histo, hist_worst)
    #axarr[1,1].set_yscale("log")

    for res in results:

        # debug output - save all gray scale images as png and name it with the blure metric
        # stretch the grayscale image to display it later
        if 0:
            gray_image = skimage.exposure.rescale_intensity(res["gray"], out_range=(100, max_adc_range-100))
            try:
                png_path = '/home/pi/ramdisk/pngs/{:.2f}.png'.format(res["blurmetric"])
                plt.imsave(png_path, gray_image, cmap='gray', vmin=0, vmax=max_adc_range)
            except Exception as e:
                print(e)

        if session["save_image_dng"]:
            writeDNGFile(res)
        if session["save_image_fits"]:
            writeFITSFile(res)


    plt.show()
