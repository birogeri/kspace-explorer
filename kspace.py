import sys
import pathlib
from uuid import uuid4

import numpy as np
import pydicom
from pydicom import errors
from PIL import Image
from PyQt5 import QtQuick
from PyQt5.QtCore import QObject, pyqtSlot, QVariant, QUrl, \
    qInstallMessageHandler
from PyQt5.QtGui import QImage, QPixmap, QColor, QIcon
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtWidgets import QApplication

# Attempting to use mkl_fft (faster FFT library for Intel CPUs). Fallback is np
try:
    import mkl_fft as m

    fft2 = m.fft2
    ifft2 = m.ifft2
except (ModuleNotFoundError, ImportError):
    fft2 = np.fft.fft2
    ifft2 = np.fft.ifft2
finally:
    fftshift = np.fft.fftshift
    ifftshift = np.fft.ifftshift


def open_file(path: str, dtype: np.dtype = np.float32) -> np.ndarray:
    """Tries to load image data into a NumPy ndarray

    The function first tries to use the PIL Image library to identify and load
    the image. PIL will convert the image to 8-bit pixels, black and white.
    If PIL fails pydicom is the next choice.

    Parameters:
        path (str): The image file location
        dtype (np.dtype): image array dtype (eg. np.float64)

    Returns:
        np.ndarray: a floating point NumPy ndarray of the specified dtype
    """

    try:
        with Image.open(path) as f:
            img_file = f.convert('F')  # 'F' mode: 32-bit floating point pixels
            img_pixel_array = np.array(img_file).astype(dtype)
        return img_pixel_array
    except FileNotFoundError:
        raise
    except OSError:
        try:
            with pydicom.dcmread(path) as dcm_file:
                img_pixel_array = dcm_file.pixel_array.astype(dtype)
            img_pixel_array.setflags(write=True)
            return img_pixel_array
        except errors.InvalidDicomError:
            try:
                raw_data = np.load(path)
                return raw_data
            except Exception as e:
                raise e


class ImageManipulators:
    """A class that contains a 2D image and kspace pair and modifier methods

    This class will load the specified image or raw data and performs any
    actions that modify the image or kspace data. A new instance should be
    initialized for new images.
    """

    def __init__(self, pixel_data: np.ndarray, is_image: bool = True):
        """Opening the image and initializing variables based on image size

        Parameters:
            pixel_data (np.ndarray): 2D pixel data of image or kspace
            is_image (bool): True if the data is an Image, false if raw data
        """

        if is_image:
            self.img = pixel_data.copy()
            self.kspacedata = np.zeros_like(self.img, dtype=np.complex64)
        else:
            self.kspacedata = pixel_data.copy()
            self.img = np.zeros_like(self.kspacedata, dtype=np.float32)

        self.image_display_data = np.require(self.img, np.uint8, 'C')
        self.kspace_display_data = np.zeros_like(self.image_display_data)
        self.orig_kspacedata = np.zeros_like(self.kspacedata)
        self.kspace_abs = np.zeros_like(self.kspacedata, dtype=np.float32)
        self.noise_map = np.zeros_like(self.kspace_abs)
        self.signal_to_noise = 30
        self.spikes = []
        self.patches = []

        if is_image:
            self.np_fft(self.img, self.kspacedata)
        else:
            self.np_ifft(self.kspacedata, self.img)

        self.orig_kspacedata[:] = self.kspacedata  # Store data write-protected
        self.orig_kspacedata.setflags(write=False)

        self.prepare_displays()

    @staticmethod
    def np_ifft(kspace: np.ndarray, out: np.ndarray):
        """Performs inverse FFT function (kspace to [magnitude] image)

        Performs iFFT on the input data and updates the display variables for
        the image domain (magnitude) image and the kspace as well.

        Parameters:
            kspace (np.ndarray): Complex kspace ndarray
            out (np.ndarray): Array to store values
        """
        np.absolute(fftshift(ifft2(ifftshift(kspace))), out=out)

    @staticmethod
    def np_fft(img: np.ndarray, out: np.ndarray):
        """ Performs FFT function (image to kspace)

        Performs FFT function, FFT shift and stores the unmodified kspace data
        in a variable and also saves one copy for display and edit purposes.

        Parameters:
            img (np.ndarray): The NumPy ndarray to be transformed
            out (np.ndarray): Array to store output (must be same shape as img)
        """
        out[:] = fftshift(fft2(ifftshift(img)))

    @staticmethod
    def normalise(f: np.ndarray):
        """ Normalises array by "streching" all values to be between 0-255.

        Parameters:
            f (np.ndarray): input array
        """
        fmin = float(f.min())
        fmax = float(f.max())
        if fmax != fmin:
            coeff = fmax - fmin
            f[:] = np.floor((f[:] - fmin) / coeff * 255.)

    @staticmethod
    def apply_window(f: np.ndarray, window_val: dict = None):
        """ Applies window values to the array

        Excludes certain values based on window width and center before
        applying normalisation on array f.
        Window values are interpreted as percentages of the maximum
        intensity of the actual image.
        For example if window_val is 1, 0.5 and image has maximum intensity
        of 196 then window width is 196, window center is 98.
        Code applied from contrib-pydicom see license below:
            Copyright (c) 2009 Darcy Mason, Adit Panchal
            This file is part of pydicom, relased under an MIT license.
            See the file LICENSE included with this distribution, also
            available at https://github.com/pydicom/pydicom
            Based on image.py from pydicom version 0.9.3,
            LUT code added by Adit Panchal

        Parameters:
            f (np.ndarray): the array to be windowed
            window_val (dict): window width and window center dict
        """
        fmax = np.max(f)
        fmin = np.min(f)
        if fmax != fmin:
            ww = (window_val['ww'] * fmax) if window_val else fmax
            wc = (window_val['wc'] * fmax) if window_val else (ww / 2)
            w_low = wc - ww / 2
            w_high = wc + ww / 2
            f[:] = np.piecewise(f, [f <= w_low, f > w_high], [0, 255,
                                lambda x: ((x - wc) / ww + 0.5) * 255])

    def prepare_displays(self, kscale: int = -3, lut: dict = None):
        """ Prepares kspace and image for display in the user interface

        Magnitude of the kspace is taken and scaling is applied for display
        purposes. This scaled representation is then transformed to a 256 color
        grayscale image by normalisation (where the highest and lowest
        intensity pixels will be intensity level 255 and 0 respectively)
        Similarly the image is prepared with the addition of windowing
        (excluding certain values based on user preference before normalisation
         eg. intensity lower than 20 and higher than 200).

        Parameters:
            kscale (int): kspace intensity scaling constant (10^kscale)
            lut (dict): window width and window center dict
        """

        # 1. Apply window to image
        self.apply_window(self.img, lut)

        # 2. Prepare kspace display - get magnitude then scale and normalise
        # K-space scaling: https://homepages.inf.ed.ac.uk/rbf/HIPR2/pixlog.htm
        np.absolute(self.kspacedata, out=self.kspace_abs)
        if self.kspace_abs.max() > 0:
            scaling_c = np.power(10., kscale)
            np.log1p(self.kspace_abs * scaling_c, out=self.kspace_abs)
            self.normalise(self.kspace_abs)

        # 3. Obtain uint8 type arrays for QML display
        self.image_display_data[:] = np.require(self.img, np.uint8)
        self.kspace_display_data[:] = np.require(self.kspace_abs, np.uint8)

    def resize_arrays(self, size: (int, int)):
        """ Resize arrays for image size changes (eg. remove kspace lines etc.)

        Called by undersampling kspace and the image_change method. If the FOV
        is modified, image_change will reset the size based on the original
        kspace, performs other modifications to the image that are applied
        before undersampling and then reapplies the size change.

        Parameters:
            size (int, int): size of the new array
        """
        self.img.resize(size)
        self.image_display_data.resize(size)
        self.kspace_display_data.resize(size)
        self.kspace_abs.resize(size)
        self.kspacedata.resize(size, refcheck=False)

    @staticmethod
    def reduced_scan_percentage(kspace: np.ndarray, percentage: float):
        """Deletes the a percentage of lines from the kspace in phase direction

        Deletes an equal number of lines from the top and bottom of kspace
        to only keep the specified percentage of sampled lines. For example if
        the image has 256 lines and percentage is 50.0 then 64 lines will be
        deleted from the top and bottom and 128 will be kept in the middle.

        Parameters:
            kspace (np.ndarray): Complex kspace data
            percentage (float): The percentage of lines sampled (0.0 - 100.0)
        """

        if int(percentage) < 100:
            percentage_delete = 1 - percentage / 100
            lines_to_delete = round(percentage_delete * kspace.shape[0] / 2)
            if lines_to_delete:
                kspace[0:lines_to_delete] = 0
                kspace[-lines_to_delete:] = 0

    @staticmethod
    def high_pass_filter(kspace: np.ndarray, radius: float):
        """High pass filter removes the low spatial frequencies from k-space

        This function deletes the center of kspace by removing values
        inside a circle of given size. The circle's radius is determined by
        the 'radius' float variable (0.0 - 100) as ratio of the lenght of
        the image diagonally.

        Parameters:
            kspace (np.ndarray): Complex kspace data
            radius (float): Relative size of the kspace mask circle (percent)
        """

        if radius > 0:
            r = np.hypot(*kspace.shape) / 2 * radius / 100
            rows, cols = np.array(kspace.shape, dtype=int)
            a, b = np.floor(np.array((rows, cols)) / 2).astype(int)
            y, x = np.ogrid[-a:rows - a, -b:cols - b]
            mask = x * x + y * y <= r * r
            kspace[mask] = 0

    @staticmethod
    def low_pass_filter(kspace: np.ndarray, radius: float):
        """Low pass filter removes the high spatial frequencies from k-space

        This function only keeps the center of kspace by removing values
        outside a circle of given size. The circle's radius is determined by
        the 'radius' float variable (0.0 - 100) as ratio of the lenght of
        the image diagonally

        Parameters:
            kspace (np.ndarray): Complex kspace data
            radius (float): Relative size of the kspace mask circle (percent)
        """

        if radius < 100:
            r = np.hypot(*kspace.shape) / 2 * radius / 100
            rows, cols = np.array(kspace.shape, dtype=int)
            a, b = np.floor(np.array((rows, cols)) / 2).astype(int)
            y, x = np.ogrid[-a:rows - a, -b:cols - b]
            mask = x * x + y * y <= r * r
            kspace[~mask] = 0

    @staticmethod
    def add_noise(kspace: np.ndarray, signal_to_noise: float,
                  current_noise: np.ndarray, generate_new_noise=False):
        """Adds random Guassian white noise to k-space

        Adds noise to the image to simulate an image with the given signal to
        noise ratio where SNR [dB] = 20log10(S/N) where S is the mean signal
        and N is the standard deviation of the noise.

        Parameters:
            kspace (np.ndarray): Complex kspace ndarray
            signal_to_noise (float): SNR in decibels (-30dB - +30dB)
            current_noise (np.ndarray): the existing noise map
            generate_new_noise (bool): flag to generate new noise map
        """

        if signal_to_noise < 30:
            if generate_new_noise:
                mean_signal = np.mean(np.abs(kspace))
                std_noise = mean_signal / np.power(10, (signal_to_noise / 20))
                current_noise[:] = std_noise * np.random.randn(*kspace.shape)
            kspace += current_noise

    @staticmethod
    def partial_fourier(kspace: np.ndarray, percentage: float, zf: bool):
        """ Partial Fourier

        Also known as half scan - only acquire a little over half of k-space
        or more and use conjugate symmetry to fill the rest.

        Parameters:
            kspace (np.ndarray): Complex k-space
            percentage (float): Sampled k-space percentage
            zf (bool): Zero-fill k-space instead of using symmetry
        """

        if int(percentage) != 100:
            percentage = 1 - percentage / 100
            rows_to_skip = round(percentage * (kspace.shape[0] / 2 - 1))
            if rows_to_skip and zf:
                # Partial Fourier (lines not acquired are filled with zeros)
                kspace[-rows_to_skip:] = 0
            elif rows_to_skip:
                # If the kspace has an even resolution then the
                # mirrored part will be shifted (k-space center signal
                # (DC signal) is off center). This determines the peak
                # position and adjusts the mirrored quadrants accordingly
                # https://www.ncbi.nlm.nih.gov/pubmed/22987283

                # Following two lines are a connoisseur's (== obscure) way of
                # returning 1 if the number is even and 0 otherwise. Enjoy!
                shift_hor = not kspace.shape[1] & 0x1  # Bitwise AND
                shift_ver = 0 if kspace.shape[0] % 2 else 1  # Ternary operator
                s = (shift_ver, shift_hor)

                # 1. Obtain a view of the array backwards (rotated 180 degrees)
                # 2. If the peak is off center horizontally (eg. number of
                #       columns or rows is even) roll lines to realign highest
                #       amplitude parts
                # 3. Do the same vertically
                kspace[-rows_to_skip:] = \
                    np.roll(kspace[::-1, ::-1], s, axis=(0, 1))[-rows_to_skip:]

                # Conjugate replaced lines
                np.conj(kspace[-rows_to_skip:], kspace[-rows_to_skip:])

    @staticmethod
    def hamming(kspace: np.ndarray):
        """ Hamming filter

        Applies a 2D Hamming filter to reduce Gibbs ringing
        References:
            https://mriquestions.com/gibbs-artifact.html
            https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4058219/
            https://www.roberthovden.com/tutorial/2015/fftartifacts.html

        Parameters:
            kspace: Complex k-space numpy.ndarray
        """
        x, y = kspace.shape
        window = np.outer(np.hamming(x), np.hamming(y))
        kspace *= window

    @staticmethod
    def undersample(kspace: np.ndarray, factor: int, compress: bool):
        """ Skipping every nth kspace line

        Simulates acquiring every nth (where n is the acceleration factor) line
        of kspace, starting from the midline. Commonly used in SENSE algorithm.

        Parameters:
            kspace: Complex k-space numpy.ndarray
            factor: Only scan every nth line (n=factor) starting from midline
            compress: compress kspace by removing empty lines (rectangular FOV)
        """
        # TODO memory optimise this (kspace sized memory created 3 times)
        if factor > 1:
            mask = np.ones(kspace.shape, dtype=bool)
            midline = kspace.shape[0] // 2
            mask[midline::factor] = 0
            mask[midline::-factor] = 0
            if compress:
                q = kspace[~mask]
                q = q.reshape(q.size // kspace.shape[1], kspace.shape[1])
                im.resize_arrays(q.shape)
                kspace[:] = q[:]
            else:
                kspace[mask] = 0

    @staticmethod
    def decrease_dc(kspace: np.ndarray, percentage: int):
        """Decreases the highest peak in kspace (DC signal)

        Parameters:
            kspace: Complex k-space numpy.ndarray
            percentage: reduce DC value by this value
        """
        x = kspace.shape[0] // 2
        y = kspace.shape[1] // 2
        kspace[x, y] *= (100 - percentage) / 100

    @staticmethod
    def apply_spikes(kspace: np.ndarray, spikes: list):
        """Overlays spikes to kspace

        Applies spikes (max value pixels) to the kspace data at the specified
        coordinates.

        Parameters:
            kspace (np.ndarray): Complex kspace ndarray
            spikes (list): coordinates for the spikes (row, column)
        """
        spike_intensity = kspace.max() * 2
        for spike in spikes:
            kspace[spike] = spike_intensity

    @staticmethod
    def apply_patches(kspace, patches: list):
        """Applies patches to kspace

         Applies patches (zero value squares) to the kspace data at the
         specified coordinates and size.

         Parameters:
             kspace (np.ndarray): Complex kspace ndarray
             patches (list): coordinates for the spikes (row, column, radius)
         """
        for patch in patches:
            x, y, size = patch[0], patch[1], patch[2]
            kspace[max(x - size, 0):x + size + 1,
                   max(y - size, 0):y + size + 1] = 0

    @staticmethod
    def filling(kspace: np.ndarray, value: float, mode: int):
        """Receives kspace filling UI changes and redirects to filling methods

        When the kspace filling simulation slider changes or simulation plays,
        this method receives the acquision phase (value: float, 0-100%)

        Parameters:
            kspace (np.ndarray): Complex kspace ndarray
            value (float): acquisition phase in percent
            mode (int): kspace filling mode
        """
        if mode == 0:  # Linear filling
            im.filling_linear(kspace, value)
        elif mode == 1:  # Centric filling
            im.filling_centric(kspace, value)
        elif mode == 2:  # Single shot EPI blipped
            im.filling_ss_epi_blipped(kspace, value)
        elif mode == 3:  # Archimedean spiral
            # im.filling_spiral(kspace, value)
            pass

    @staticmethod
    def filling_linear(kspace: np.ndarray, value: float):
        """Linear kspace filling

        Starts with the top left corner and sequentially fills kspace from
        top to bottom
        Parameters:
            kspace (np.ndarray): Complex kspace ndarray
            value (float): acquisition phase in percent
        """
        kspace.flat[int(kspace.size * value // 100)::] = 0

    @staticmethod
    def filling_centric(kspace: np.ndarray, value: float):
        """ Centric filling method

        Fills the center line first from left to right and then alternating one
        line above and one below.
        """
        ksp_centric = np.zeros_like(kspace)

        # reorder
        ksp_centric[0::2] = kspace[kspace.shape[0] // 2::]
        ksp_centric[1::2] = kspace[kspace.shape[0] // 2 - 1::-1]

        ksp_centric.flat[int(kspace.size * value / 100)::] = 0

        # original order
        kspace[(kspace.shape[0]) // 2 - 1::-1] = ksp_centric[1::2]
        kspace[(kspace.shape[0]) // 2::] = ksp_centric[0::2]

    @staticmethod
    def filling_ss_epi_blipped(kspace: np.ndarray, value: float):
        # Single-shot blipped EPI (zig-zag pattern)
        # https://www.imaios.com/en/e-Courses/e-MRI/MRI-Sequences/echo-planar-imaging
        ksp_epi = np.zeros_like(kspace)
        ksp_epi[::2] = kspace[::2]
        ksp_epi[1::2] = kspace[1::2, ::-1]  # Every second line backwards

        ksp_epi.flat[int(kspace.size * value / 100)::] = 0

        kspace[::2] = ksp_epi[::2]
        kspace[1::2] = ksp_epi[1::2, ::-1]


class MainApp(QObject):
    """ Main App
    This class handles all interaction with the QML user interface
    """

    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.win = parent
        self.ctx = context

        def bind(object_name: str) -> QtQuick.QQuickItem:
            """Finds the QML Object with the object name

            Parameters:
                object_name (str): UI element's objectName in QML file

            Returns:
                QQuickItem: Reference to the QQuickItem found by the function
            """
            return win.findChild(QObject, object_name)

        # List of QML control objectNames that we will bind to
        ctrls = ["image_display", "kspace_display", "noise_slider", "compress",
                 "decrease_dc", "partial_fourier_slider", "undersample_kspace",
                 "high_pass_slider", "low_pass_slider", "ksp_const", "filling",
                 "hamming", "rdc_slider", "zero_fill", "compress", "droparea",
                 "filling_mode", "thumbnails"]

        # Binding UI elements and controls
        for ctrl in ctrls:
            setattr(self, "ui_" + ctrl, bind(ctrl))

        # Initialise an empty list of image paths that can later be filled
        self.url_list = []
        self.current_img = 0
        self.file_data = []
        self.is_image = True
        self.channels = 1
        self.img_instances = {}

    def execute_load(self):
        """ Replaces the ImageManipulators class therefore changing the image

        Can be called by changing the image list (new image(s) opened) or by
        flipping through the existing list of images. If the image is not
        accessible or does not contain an image, it is removed from the list.
        """
        global im
        try:
            path = self.url_list[self.current_img]
            self.file_data = open_file(path)
            self.is_image = False if len(self.file_data.shape) > 2 else True
        except (FileNotFoundError, ValueError):
            # When the image is inaccessible at load time
            del self.url_list[self.current_img]

        if self.is_image:
            self.channels = 0
            self.img_instances = {}
            im = ImageManipulators(self.file_data, self.is_image)
        else:
            self.channels = self.file_data.shape[0]
            for channel in range(self.channels):
                # Extract 2D data slices from 3D array
                file_data = self.file_data[channel, :, :]
                self.img_instances[channel] = \
                    ImageManipulators(file_data, self.is_image)
            im = self.img_instances[0]

        # Let the QML thumbnails list know about the number of channels
        self.ui_thumbnails.setProperty("model", self.channels)

        self.update_displays()

        self.ui_droparea.setProperty("loaded_imgs", len(self.url_list))
        self.ui_droparea.setProperty("curr_img", self.current_img + 1)

    @pyqtSlot(str, name="load_new_img")
    def load_new_img(self, urls: str):
        """ Image loader

        Loads an image from the specified path

        Parameters:
            urls: list of QUrls to be opened
        """

        # Two or more files dropped become comma separated string of urls.
        self.current_img = 0
        self.url_list = urls.split(",")
        self.url_list[:] = [s.replace('file:///', '') for s in self.url_list]
        self.ui_droparea.setProperty("loaded_imgs", len(self.url_list))
        self.ui_droparea.setProperty("curr_img", self.current_img + 1)
        self.execute_load()

    @pyqtSlot(bool, name="wheel_img")
    def next_img(self, up: bool):
        """ Steps to the next image on mousewheel event

        Parameters:
            up (bool): True if mousewheel moves up

        """
        if len(self.url_list):
            self.current_img += 1 if up else -1
            self.current_img %= len(self.url_list)
            self.execute_load()

    @pyqtSlot(int, name="channel_change")
    def channel_change(self, channel: int):
        """ Called when channel is selected in the thumbnails bar

        Parameters:
            channel (int): Index of the selected channel

        """
        global im
        im = self.img_instances[int(channel)]
        self.update_displays()

    @pyqtSlot(str, name="save_img")
    def save_img(self, path):
        """Saves the visible kspace and image to files

        Saves the 32 bit/pixel image if TIFF format is selected otherwise
        the PNG file will have a depth of 8 bits.

        Parameters:
            path (str): QUrl format file location (starts with "file:///")
        """
        import os.path
        filename, ext = os.path.splitext(path[8:])  # Remove QUrl's "file:///"
        k_path = filename + '_k' + ext
        i_path = filename + '_i' + ext
        if ext.lower() == '.tiff':
            Image.fromarray(im.img).save(i_path)
            Image.fromarray(im.kspace_display_data).save(k_path)
        elif ext == '.png':
            Image.fromarray(im.img).convert(mode='L').save(i_path)
            Image.fromarray(im.kspace_display_data).convert(mode='L').save(
                k_path)

    @pyqtSlot(QVariant, QVariant, name="add_spike")
    def add_spike(self, mouse_x, mouse_y):
        """Inserts a spike at a location given by the UI.

        Values are saved in reverse order because NumPy's indexing conventions:
        array[row (== y), column (== x)]

        Parameters:
            mouse_x: click position on the x-axis
            mouse_y: click position on the y-axis
        """
        im.spikes.append((int(mouse_y), int(mouse_x)))

    @pyqtSlot(QVariant, QVariant, QVariant, name="add_patch")
    def add_patch(self, mouse_x, mouse_y, radius):
        """Inserts a patch at a location given by the UI.

        Values are saved in reverse order because NumPy's indexing conventions:
        array[row (== y), column (== x)]

        Parameters:
            mouse_x: click position on the x-axis
            mouse_y: click position on the y-axis
            radius: size of the patch
        """
        im.patches.append((int(mouse_y), int(mouse_x), radius))

    @pyqtSlot(name="delete_spikes")
    def delete_spikes(self):
        """Deletes manually added kspace spikes"""
        im.spikes = []

    @pyqtSlot(name="delete_patches")
    def delete_patches(self):
        """Deletes manually added kspace patches"""
        im.patches = []

    @pyqtSlot(name="undo_patch")
    def undo_patch(self):
        """Deletes the last patch"""
        if im.patches:
            del im.patches[-1]

    @pyqtSlot(name="undo_spike")
    def undo_spike(self):
        """Deletes the last spike"""
        if im.spikes:
            del im.spikes[-1]

    @pyqtSlot(name="update_displays")
    def update_displays(self):
        """Triggers modifiers to kspace and updates the displays"""
        self.image_change()

        # Replacing image source for QML Image elements - this will trigger
        # requestPixmap. The image name must be different for Qt to display the
        # new one, so a random string is appended to the end
        self.ui_kspace_display. \
            setProperty("source", "image://imgs/kspace_%s" % uuid4().hex)
        self.ui_image_display. \
            setProperty("source", "image://imgs/image_%s" % uuid4().hex)

        #  Iterate through thumbnails and set source image to trigger reload
        for item in self.ui_thumbnails.childItems()[0].childItems():
            try:
                oname = item.childItems()[0].property("objectName")
                source = "image://imgs/" + oname + "_%s" % uuid4().hex
                item.childItems()[0].setProperty("source", source)
            except IndexError:
                # Highlight component of the ListView does not have childItems
                pass

    def image_change(self):
        """ Apply kspace modifiers to kspace and get resulting image"""

        # Get a copy of the original k-space data to play with
        im.resize_arrays(im.orig_kspacedata.shape)
        im.kspacedata[:] = im.orig_kspacedata

        # 01 - Noise
        new_snr = self.ui_noise_slider.property('value')
        generate_new = False
        if new_snr != im.signal_to_noise:
            generate_new = True
            im.signal_to_noise = new_snr
        im.add_noise(im.kspacedata, new_snr, im.noise_map, generate_new)

        # 02 - Spikes
        im.apply_spikes(im.kspacedata, im.spikes)

        # 03 - Patches
        im.apply_patches(im.kspacedata, im.patches)

        # 04 - Reduced scan percentage
        if self.ui_rdc_slider.property("enabled"):
            v_ = self.ui_rdc_slider.property("value")
            im.reduced_scan_percentage(im.kspacedata, v_)

        # 05 - Partial fourier
        if self.ui_partial_fourier_slider.property("enabled"):
            v_ = self.ui_partial_fourier_slider.property("value")
            zf = self.ui_zero_fill.property("checked")
            im.partial_fourier(im.kspacedata, v_, zf)

        # 06 - High pass filter
        v_ = self.ui_high_pass_slider.property("value")
        im.high_pass_filter(im.kspacedata, v_)

        # 07 - Low pass filter
        v_ = self.ui_low_pass_slider.property("value")
        im.low_pass_filter(im.kspacedata, v_)

        # 08 - Undersample k-space
        v_ = self.ui_undersample_kspace.property("value")
        if int(v_):
            compress = self.ui_compress.property("checked")
            im.undersample(im.kspacedata, int(v_), compress)

        # 09 - DC signal decrease
        v_ = self.ui_decrease_dc.property("value")
        if int(v_) > 1:
            im.decrease_dc(im.kspacedata, int(v_))

        # 10 - Hamming filter
        if self.ui_hamming.property("checked"):
            im.hamming(im.kspacedata)

        # 11 - Acquisition simulation progress
        if self.ui_filling.property("value") < 100:
            mode = self.ui_filling_mode.property("currentIndex")
            im.filling(im.kspacedata, self.ui_filling.property("value"), mode)

        # Get the resulting image
        im.np_ifft(kspace=im.kspacedata, out=im.img)

        # Get display properties
        kspace_const = int(self.ui_ksp_const.property('value'))
        # Window values
        ww = self.ui_image_display.property("ww")
        wc = self.ui_image_display.property("wc")
        win_val = {'ww': ww, 'wc': wc}
        im.prepare_displays(kspace_const, win_val)


class ImageProvider(QtQuick.QQuickImageProvider):
    """
    Contains the interface between numpy and qt
    Qt calls MainApp.update_displays on UI change
    that method requests new images to display
    pyqt channels it back to Qt GUI

    """

    def __init__(self):
        QtQuick.QQuickImageProvider. \
            __init__(self, QtQuick.QQuickImageProvider.Pixmap)

    def requestPixmap(self, id_str: str, requested_size):
        """Qt calls this function when an image changes

        Parameters:
            id_str: identifies the requested image
            requested_size: image size requested by QML (usually ignored)

        Returns:
            QPixmap: an image in the format required by Qt
        """
        try:
            if id_str.startswith('image'):
                q_im = QImage(im.image_display_data,             # data
                              im.image_display_data.shape[1],    # width
                              im.image_display_data.shape[0],    # height
                              im.image_display_data.strides[0],  # bytes/line
                              QImage.Format_Grayscale8)          # format

            elif id_str.startswith('kspace'):
                q_im = QImage(im.kspace_display_data,             # data
                              im.kspace_display_data.shape[1],    # width
                              im.kspace_display_data.shape[0],    # height
                              im.kspace_display_data.strides[0],  # bytes/line
                              QImage.Format_Grayscale8)           # format

            elif id_str.startswith('thumb'):
                thumb_id = int(id_str[6:6+id_str[6:].find('_')])
                im_c = py_mainapp.img_instances[thumb_id]
                q_im = QImage(im_c.image_display_data,             # data
                              im_c.image_display_data.shape[1],    # width
                              im_c.image_display_data.shape[0],    # height
                              im_c.image_display_data.strides[0],  # bytes/line
                              QImage.Format_Grayscale8)            # format

            else:
                raise NameError

        except NameError:
            # On error we return a red image of requested size
            q_im = QPixmap(requested_size)
            q_im.fill(QColor('red'))

        return QPixmap(q_im), QPixmap(q_im).size()


if __name__ == "__main__":
    # Handling QML messages and catching Python exceptions

    def qt_message_handler(mode, context, message):
        # https://doc.qt.io/qt-5/qtglobal.html#QtMsgType-enum
        modes = ['Debug', 'Warning', 'Critical', 'Fatal', 'Info']
        print("%s: %s (%s:%d, %s)" % (
            modes[mode], message, context.file, context.line, context.file))

    qInstallMessageHandler(qt_message_handler)

    # Loading resources
    import qrc

    default_image = 'images/default.dcm'
    app_path = pathlib.Path(__file__).parent.absolute()
    default_image = str(app_path.joinpath(default_image))

    # Main application start
    app = QApplication([])

    app.setWindowIcon(QIcon(':/images/icon.ico'))
    app.setOrganizationName("K-space Explorer")
    app.setOrganizationDomain("k-space.app")
    app.setApplicationName("K-space Explorer")

    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()

    # Image manipulator and storage initialisation with default image
    engine.addImageProvider("imgs", ImageProvider())
    im = ImageManipulators(open_file(default_image), is_image=True)

    # Loading GUI file
    # engine.load('ui_source/ui.qml')
    engine.load(QUrl('qrc:/ui.qml'))

    win = engine.rootObjects()[0]
    py_mainapp = MainApp(ctx, win)
    ctx.setContextProperty("py_MainApp", py_mainapp)

    win.show()

    sys.exit(app.exec_())
