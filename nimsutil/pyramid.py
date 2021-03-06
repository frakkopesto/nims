#!/usr/bin/env python
#
# @author:  Bob Dougherty
#           Gunnar Schaefer

"""
The CNI pyramid viewer uses PanoJS for the front-end.
See: http://www.dimin.net/software/panojs/
"""

from __future__ import print_function

import os
import argparse

import math
import Image
import numpy
import nibabel


class ImagePyramidError(Exception):
    pass


class ImagePyramid(object):

    """
    Generate a panojs-style image pyramid of a 2D montage of slices from a >=3D dataset (usually a NIfTI file).

    Example:
        import pyramid
        pyr = pyramid.ImagePyramid('t1.nii.gz')
        pyr.generate()
    """

    def __init__(self, filename, tile_size=256, log=None):
        self.data = nibabel.load(filename).get_data()
        self.tile_size = tile_size
        self.log = log

    def generate(self, outdir, panojs_url='https://cni.stanford.edu/js/panojs/'):
        """
        Generate a multi-resolution image pyramid, using generate_pyramid(), and the corresponding
        viewer HTML file, using generate_viewer().
        """
        self.generate_montage()
        viewer_file = os.path.join(outdir, 'index.html')
        try:
            self.generate_pyramid(outdir)
        except ImagePyramidError as e:
            self.log and self.log.error(e.message) or print(e.message)
            with open(viewer_file, 'w') as f:
                f.write('<body>\n<center>Image viewer could not be generated for this dataset. (' + e.message + ')</center>\n</body>\n')
        else:
            self.generate_viewer(viewer_file, panojs_url)

    def generate_pyramid(self, outdir):
        """
        Slice up a NIfTI file into a multi-res pyramid of tiles.
        We use the file name convention suitable for PanoJS (http://www.dimin.net/software/panojs/):
        The zoom level (z) is an integer between 0 and n, where 0 is fully zoomed in and n is zoomed out.
        E.g., z=n is for 1 tile covering the whole world, z=n-1 is for 2x2=4 tiles, ... z=0 is the original resolution.
        """
        if not os.path.exists(outdir): os.makedirs(outdir)
        sx,sy = self.montage.size
        if sx*sy<1:
            raise ImagePyramidError('degenerate image size (%d,%d); no tiles will be created' % (sx, sy))

        divs = int(numpy.ceil(numpy.log2(float(max(sx,sy))/self.tile_size)))
        for iz in range(divs+1):
            z = divs - iz
            ysize = int(round(float(sy)/pow(2,iz)))
            xsize = int(round(float(ysize)/sy*sx))
            xpieces = int(math.ceil(float(xsize)/self.tile_size))
            ypieces = int(math.ceil(float(ysize)/self.tile_size))
            self.log or print('level %s, size %dx%d, splits %d,%d' % (z, xsize, ysize, xpieces, ypieces))
            # TODO: we don't need to use 'thumbnail' here. This function always returns a square
            # image of the requested size, padding and scaling as needed. Instead, we should resize
            # and chop the image up, with no padding, ever. panojs can handle non-square images
            # at the edges, so the padding is unnecessary and, in fact, a little wrong.
            im = self.montage.copy()
            im.thumbnail([xsize,ysize], Image.ANTIALIAS)
            # Convert the image to grayscale
            im = im.convert("L")
            for x in range(xpieces):
                for y in range(ypieces):
                    tile = im.copy().crop((x*self.tile_size, y*self.tile_size, min((x+1)*self.tile_size,xsize), min((y+1)*self.tile_size,ysize)))
                    tile.save(os.path.join(outdir, ('%03d_%03d_%03d.jpg' % (iz,x,y))), "JPEG", quality=85)

    def generate_viewer(self, outfile, panojs_url):
        """
        Creates a baisc html file for viewing the image pyramid with panojs.
        """
        (x_size,y_size) = self.montage.size
        with open(outfile, 'w') as f:
            f.write('<head>\n<meta http-equiv="imagetoolbar" content="no"/>\n')
            f.write('<style type="text/css">@import url(' + panojs_url + 'styles/panojs.css);</style>\n')
            f.write('<script type="text/javascript" src="' + panojs_url + 'extjs/ext-core.js"></script>\n')
            f.write('<script type="text/javascript" src="' + panojs_url + 'panojs/utils.js"></script>\n')
            f.write('<script type="text/javascript" src="' + panojs_url + 'panojs/PanoJS.js"></script>\n')
            f.write('<script type="text/javascript" src="' + panojs_url + 'panojs/controls.js"></script>\n')
            f.write('<script type="text/javascript" src="' + panojs_url + 'panojs/pyramid_imgcnv.js"></script>\n')
            f.write('<script type="text/javascript" src="' + panojs_url + 'panojs/control_thumbnail.js"></script>\n')
            f.write('<script type="text/javascript" src="' + panojs_url + 'panojs/control_info.js"></script>\n')
            f.write('<script type="text/javascript" src="' + panojs_url + 'panojs/control_svg.js"></script>\n')
            f.write('<script type="text/javascript" src="' + panojs_url + 'viewer.js"></script>\n')
            f.write('<style type="text/css">body { font-family: sans-serif; margin: 0; padding: 10px; color: #000000; background-color: #FFFFFF; font-size: 0.7em; } </style>\n')
            f.write('<script type="text/javascript">\nvar viewer = null;Ext.onReady(function () { createViewer( viewer, "viewer", ".", "", '+str(self.tile_size)+', '+str(x_size)+', '+str(y_size)+' ) } );\n</script>\n')
            f.write('</head>\n<body>\n')
            f.write('<div style="width: 100%; height: 100%;"><div id="viewer" class="viewer" style="width: 100%; height: 100%;" ></div></div>\n')
            f.write('</body>\n</html>\n')

    def generate_montage(self):
        """Full-sized montage of the entire numpy data array."""
        # Figure out the image dimensions and make an appropriate montage.
        # NIfTI images can have up to 7 dimensions. The fourth dimension is
        # by convention always supposed to be time, so some images (RGB, vector, tensor)
        # will have 5 dimensions with a single 4th dimension. For our purposes, we
        # can usually just collapse all dimensions above the 3rd.
        # TODO: we should handle data_type = RGB as a special case.
        # TODO: should we use the scaled data (getScaledData())? (We do some auto-windowing below)

        data = self.data.squeeze()
        # TODO: "percentile" is very slow for large arrays. Is there a short cut that we can use?
        # Maybe try taking a smaller subset of the array?
        if data.dtype != 'uint8':
            # Auto-window the data by clipping values above and below the following thresholds, then scale to unit8.
            clip_vals = numpy.percentile(data, (20.0, 99.0))
            data = data.clip(clip_vals[0], clip_vals[1]) - clip_vals[0]
            data = numpy.cast['uint8'](numpy.round(data/(clip_vals[1]-clip_vals[0])*255.0))
        # This transpose (usually) makes the resulting images come out in a more standard orientation.
        # TODO: we could look at the qto_xyz to infer the optimal transpose for any dataset.
        data = data.transpose(numpy.concatenate(([1,0],range(2,data.ndim))))
        num_images = numpy.prod(data.shape[2:])

        if data.ndim < 2:
            raise Exception('NIfTI file must have at least 2 dimensions')
        elif data.ndim == 2:
            # a single slice: no need to do anything
            num_cols = 1;
            data = numpy.atleast_3d(data)
        elif data.ndim == 3:
            # a simple (x, y, z) volume- set num_cols to produce a square(ish) montage.
            rows_to_cols_ratio = float(data.shape[0])/float(data.shape[1])
            num_cols = int(math.ceil(math.sqrt(float(num_images)) * math.sqrt(rows_to_cols_ratio)))
        elif data.ndim >= 4:
            # timeseries (x, y, z, t) or more
            num_cols = data.shape[2]
            data = data.transpose(numpy.concatenate(([0,1,3,2],range(4,data.ndim)))).reshape(data.shape[0], data.shape[1], num_images)

        num_rows = int(numpy.ceil(float(data.shape[2])/float(num_cols)))
        montage_array = numpy.zeros((data.shape[0] * num_rows, data.shape[1] * num_cols), dtype=numpy.uint8)
        for im_num in range(data.shape[2]):
            slice_r, slice_c = im_num/num_cols * data.shape[0], im_num%num_cols * data.shape[1]
            montage_array[slice_r:slice_r + data.shape[0], slice_c:slice_c + data.shape[1]] = data[:, :, im_num]
        self.montage = Image.fromarray(montage_array)
        # NOTE: the following will crop away edges that contain only zeros. Not sure if we want this.
        self.montage = self.montage.crop(self.montage.getbbox())


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self):
        super(ArgumentParser, self).__init__()
        self.description = """Create a panojs-style image pyramid from a NIfTI file."""
        self.add_argument('-p', '--panojs_url', metavar='URL', help='URL for the panojs javascript.')
        self.add_argument('filename', help='path to NIfTI file')
        self.add_argument('outdir', nargs='?', help='output directory')


if __name__ == '__main__':
    args = ArgumentParser().parse_args()
    outdir = args.outdir or os.path.basename(os.path.splitext(os.path.splitext(args.filename)[0])[0]) + '.pyr'

    pyr = ImagePyramid(args.filename)
    pyr.generate(outdir, args.panojs_url) if args.panojs_url else pyr.generate(outdir)
