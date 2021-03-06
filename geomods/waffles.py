### waffles.py
##
## Copyright (c) 2010 - 2020 CIRES Coastal DEM Team
##
## Permission is hereby granted, free of charge, to any person obtaining a copy 
## of this software and associated documentation files (the "Software"), to deal 
## in the Software without restriction, including without limitation the rights 
## to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies 
## of the Software, and to permit persons to whom the Software is furnished to do so, 
## subject to the following conditions:
##
## The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
##
## THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, 
## INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR 
## PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE 
## FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, 
## ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
##
### Commentary:
## WAFFLES - Generate Digital Elevation Models and derivatives using a variety of algorithms, etc.
##
## GDAL and gdal-python are required to run waffles.
##
## Recommended external software for full functionality:
## - GMT (FOSS)
## - MBSystem (FOSS)
## - VDatum (US/NOAA)
## - LASTools (Non-Free) - data processing
##
## see/set `_waffles_grid_info` dictionary to run a grid.
##
## Current DEM modules:
## surface (GMT), triangulate (GMT/GDAL), nearneighbor (GMT/GDAL), mbgrid (MBSYSTEM), num (waffles), average (GDAL)
##
## optionally, clip, filter, buffer the resulting DEM.
##
## find data to grid with GEOMODS' fetch.py
##
## DATALIST and REGION functions - datalists.py
##
## MBSystem/Waffles style datalists.
## Recurse through a datalist file and process the results.
##
## a datalist '*.datalist' file should be formatted as in MBSystem:
## ~path ~format ~weight ~metadata,list ~etc
##
## a format of -1 represents a datalist
## a format of 168 represents XYZ data
## a format of 200 represents GDAL data
## a format of 300 represents LAS/LAZ data <not implemented>
## a format of 400 represents a FETCHES module - e.g. `nos:datatype=bag`
##
## each xyz file in a datalist should have an associated '*.inf' file 
## for faster processing
##
## 'inf' files can be generated using 'mbdatalist -O -V -I~datalist.datalist'
## or via `waffles -M datalists:infos=True`
##
## GDAL/LIDAR/FETCHES data don't need inf files.
##
## if 'region' is specified, will only process data that falls within
## the given region
##
### TODO:
## Add remove/replace module
## Add source uncertainty to uncertainty module
## Add LAS/LAZ support to datalits
## -W weight-range for datalist processing
## -B for 'breakline' (densify line/exract nodes/add to datalist)
##
### Code:
import sys
import os
import io
import time
import glob
import math
import copy
import shutil
import subprocess
## ==============================================
## import gdal, etc.
## ==============================================
import numpy as np
import json
import gdal
import ogr
import osr

#from geomods import fetches
import fetches

## ==============================================
## General utility functions - utils.py
## ==============================================
_version = '0.5.7'

def inc2str_inc(inc):
    '''convert a WGS84 geographic increment to a str_inc (e.g. 0.0000925 ==> `13`)

    returns a str representation of float(inc)'''
    
    import fractions
    return(str(fractions.Fraction(str(inc * 3600)).limit_denominator(10)).replace('/', ''))

def this_date():
    '''return the current date'''
    
    import datetime
    return(datetime.datetime.now().strftime('%Y%m%d%H%M%S'))

def this_year():
    '''return the current year'''
    
    import datetime
    return(datetime.datetime.now().strftime('%Y'))

def rm_f(f_str):
    '''os.remove f_str, pass if error'''
    
    try:
        if os.path.exists(f_str):
            os.remove(f_str)
    except: pass
    return(0)
        
def remove_glob(glob_str):
    '''glob `glob_str` and os.remove results, pass if error'''

    try:
        globs = glob.glob(glob_str)
    except: globs = None
    if globs is None: return(0)
    for g in globs:
        try:
            os.remove(g)
        except: pass
    return(0)

def args2dict(args, dict_args = {}):
    '''convert list of arg strings to dict.
    args are a list of ['key=val'] pairs

    returns a dictionary of the key/values'''
    
    for arg in args:
        p_arg = arg.split('=')
        dict_args[p_arg[0]] = False if p_arg[1].lower() == 'false' else True if p_arg[1].lower() == 'true' else None if p_arg[1].lower() == 'none' else p_arg[1]
    return(dict_args)

def int_or(val, or_val = None):
    '''returns val as int otherwise returns or_val'''
    
    try:
        return(int(val))
    except: return(or_val)

def hav_dst(pnt0, pnt1):
    '''return the distance between pnt0 and pnt1,
    using the haversine formula.
    `pnts` are geographic and result is in meters.'''
    
    x0 = float(pnt0[0])
    y0 = float(pnt0[1])
    x1 = float(pnt1[0])
    y1 = float(pnt1[1])
    rad_m = 637100
    dx = math.radians(x1 - x0)
    dy = math.radians(y1 - y0)
    a = math.sin(dx / 2) * math.sin(dx / 2) + math.cos(math.radians(x0)) * math.cos(math.radians(x1)) * math.sin(dy / 2) * math.sin(dy / 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return(rad_m * c)

def path_exists_or_url(src_str):
    if os.path.exists(src_str): return(True)
    if src_str[:4] == 'http': return(True)
    if src_str.split(':')[0] in _known_datalist_fmts[400]: return(True)
    echo_error_msg('invalid datafile/datalist: {}'.format(src_str))
    return(False)

def _clean_zips(zip_files):
    '''remove all files\directories in `zip_files`'''

    for i in zip_files:
        if os.path.isfile(i):
            os.remove(i)
            zip_files = [x for x in zip_files if x != i]
    if len(zip_files) > 0:
        for i in zip_files:
            if os.path.isdir(i):
                try:
                    os.removedirs(i)
                except: pass
    return(0)

def unzip(zip_file):
    '''unzip (extract) `zip_file` and return a list of extracted file names.'''
    
    import zipfile
    zip_ref = zipfile.ZipFile(zip_file)
    zip_files = zip_ref.namelist()
    zip_ref.extractall()
    zip_ref.close()
    return(zip_files)

def gunzip(gz_file):
    '''gunzip `gz_file` and return the extracted file name.'''
    
    import gzip
    if os.path.exists(gz_file):
        gz_split = gz_file.split('.')[:-1]
        guz_file = '{}.{}'.format(gz_split[0], gz_split[1])
        with gzip.open(gz_file, 'rb') as in_gz, \
             open(guz_file, 'wb') as f:
            while True:
                block = in_gz.read(65536)
                if not block:
                    break
                else: f.write(block)
    else:
        echo_error_msg('{} does not exist'.format(gz_file))
        guz_file = None
    return(guz_file)

def procs_unzip(src_file, exts):
    '''unzip/gunzip self.src_file and return the file associated with `exts`'''

    zips = []
    src_proc = None
    if src_file.split('.')[-1] == 'zip':
        zips = unzip(src_file)
        for ext in exts:
            for zf in zips:
                if ext in zf:
                    src_proc = zf
                    break
                #else: remove_glob(zf)
    elif src_file.split('.')[-1] == 'gz':
        tmp_proc = gunzip(src_file)
        if tmp_proc is not None:
            for ext in exts:
                if ext in tmp_proc:
                    src_proc = os.path.basename(tmp_proc)
                    os.rename(tmp_proc, src_proc)
                    break
    else:
        for ext in exts:
            if ext in src_file:
                src_proc = src_file
                break
    return([src_proc, zips])

def err_fit_plot(xdata, ydata, out, fitfunc, dst_name = 'unc', xa = 'distance'):
    '''plot a best fit plot'''
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.offsetbox import AnchoredText
    except: echo_error_msg('you need to install matplotlib to run uncertainty plots...')
    
    plt.plot(xdata, ydata, 'o')
    plt.plot(xdata, fitfunc(out, xdata), '-')
    plt.xlabel(xa)
    plt.ylabel('error (m)')
    out_png = '{}_bf.png'.format(dst_name)
    plt.savefig(out_png)
    plt.close()

def err_scatter_plot(error_arr, dist_arr, dst_name = 'unc', xa = 'distance'):
    '''plot a scatter plot'''
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.offsetbox import AnchoredText
    except: echo_error_msg('you need to install matplotlib to run uncertainty plots...')

    plt.scatter(dist_arr, error_arr)
    #plt.title('Scatter')
    plt.xlabel(xa)
    plt.ylabel('error (m)')
    out_png = '{}_scatter.png'.format(dst_name)
    plt.savefig(out_png)
    plt.close()

def err2coeff(err_arr, coeff_guess = [0, 0.1, 0.2], dst_name = 'unc', xa = 'distance'):
    '''calculate and plot the error coefficient given err_arr which is 
    a 2 col array with `err dist`'''

    from scipy import optimize

    error = err_arr[:,0]
    distance = err_arr[:,1]
    
    max_int_dist = np.max(distance)
    nbins = 10
    n, _ = np.histogram(distance, bins = nbins)
    # want at least 2 values in each bin?
    while 0 or 1 in n:
        nbins -= 1
        n, _ = np.histogram(distance, bins = nbins)
    serror, _ = np.histogram(distance, bins = nbins, weights = error)
    serror2, _ = np.histogram(distance, bins = nbins, weights = error**2)
    mean = serror / n
    std = np.sqrt(serror2 / n - mean * mean)
    ydata = np.insert(std, 0, 0)
    bins_orig=(_[1:] + _[:-1]) / 2
    xdata = np.insert(bins_orig, 0, 0)
    fitfunc = lambda p, x: p[0] + p[1] * (abs(x) ** abs(p[2]))
    errfunc = lambda p, x, y: y - fitfunc(p, x)
    out, cov, infodict, mesg, ier = optimize.leastsq(errfunc, coeff_guess, args = (xdata, ydata), full_output = True)
    err_fit_plot(xdata, ydata, out, fitfunc, dst_name, xa)
    err_scatter_plot(error, distance, dst_name, xa)
    return(out)

#def err_plot(err_arr, d_max, dst_name = 'unc'):
    #'''plot a numpy array of 'err dist' values and return the error coefficient.'''    
    #err_arr = err_arr[err_arr[:,1] < d_max,:]
    #err_arr = err_arr[err_arr[:,1] > 0,:]
    #return(err2coeff(err_arr, dst_name = dst_name))

## ==============================================
## system cmd verification and configs.
## ==============================================
cmd_exists = lambda x: any(os.access(os.path.join(path, x), os.X_OK) for path in os.environ['PATH'].split(os.pathsep))

def run_cmd(cmd, data_fun = None, verbose = False):
    '''Run a system command while optionally passing data.
    `data_fun` should be a function to write to a file-port:
    >> data_fun = lambda p: datalist_dump(wg, dst_port = p, ...)

    returns [command-output, command-return-code]'''
    
    if verbose: echo_msg('running cmd: {}...'.format(cmd.rstrip()))    
    if data_fun is not None:
        pipe_stdin = subprocess.PIPE
    else: pipe_stdin = None
    p = subprocess.Popen(cmd, shell = True, stdin = pipe_stdin, stdout = subprocess.PIPE, stderr = subprocess.PIPE, close_fds = True)    

    if data_fun is not None:
        if verbose: echo_msg('piping data to cmd subprocess...')
        data_fun(p.stdin)
        p.stdin.close()
    
    while p.poll() is None:
        if verbose:
            rl = p.stderr.readline()
            sys.stderr.write('\x1b[2K\r')
            sys.stderr.write(rl.decode('utf-8'))
    if verbose: sys.stderr.write(p.stderr.read().decode('utf-8'))

    out = p.stdout.read()
    p.stderr.close()
    p.stdout.close()
    if verbose: echo_msg('ran cmd: {} and returned {}.'.format(cmd.rstrip(), p.returncode))
    return(out, p.returncode)

def yield_cmd(cmd, data_fun = None, verbose = False):
    '''Run a system command while optionally passing data.
    `data_fun` should be a function to write to a file-port:
    >> data_fun = lambda p: datalist_dump(wg, dst_port = p, ...)

    returns [command-output, command-return-code]'''
    
    if verbose: echo_msg('running cmd: {}...'.format(cmd.rstrip()))    
    if data_fun is not None:
        pipe_stdin = subprocess.PIPE
    else: pipe_stdin = None
    p = subprocess.Popen(cmd, shell = True, stdin = pipe_stdin, stdout = subprocess.PIPE, stderr = subprocess.PIPE, close_fds = True)    

    if data_fun is not None:
        if verbose: echo_msg('piping data to cmd subprocess...')
        data_fun(p.stdin)
        p.stdin.close()

    while True:
        line = p.stdout.readline().decode('utf-8')
        #sys.stderr.write(line.decode('utf-8'))
        if not line: break
        else: yield(line)
    p.stdout.close()
    if verbose: echo_msg('ran cmd: {} and returned {}.'.format(cmd.rstrip(), p.returncode))

def cmd_check(cmd_str, cmd_vers_str):
    '''check system for availability of 'cmd_str' 

    returns the commands version or None'''
    
    if cmd_exists(cmd_str): 
        cmd_vers, status = run_cmd('{}'.format(cmd_vers_str))
        return(cmd_vers.rstrip())
    else: return(None)

def config_check(chk_vdatum = False, verbose = False):
    '''check for needed waffles external software.
    waffles external software: gdal, gmt, mbsystem
    also checks python version and host OS and 
    records waffles version

    returns a dictionary of gathered results.'''
    
    _waff_co = {}
    py_vers = str(sys.version_info[0]),
    host_os = sys.platform
    _waff_co['platform'] = host_os
    _waff_co['python'] = py_vers[0]
    ae = '.exe' if host_os == 'win32' else ''

    #if chk_vdatum: _waff_co['VDATUM'] = vdatum(verbose=verbose).vdatum_path
    _waff_co['GDAL'] = cmd_check('gdal_grid{}'.format(ae), 'gdal_grid --version')
    _waff_co['GMT'] = cmd_check('gmt{}'.format(ae), 'gmt --version')
    _waff_co['MBGRID'] = cmd_check('mbgrid{}'.format(ae), 'mbgrid -version | grep Version')
    _waff_co['WAFFLES'] = str(_version)
    return(_waff_co)
    
## ==============================================
## stderr messaging
## ==============================================
def echo_error_msg2(msg, prefix = 'waffles'):
    '''echo error msg to stderr using `prefix`
    >> echo_error_msg2('message', 'test')
    test: error, message'''
    
    sys.stderr.write('\x1b[2K\r')
    sys.stderr.flush()
    sys.stderr.write('{}: error, {}\n'.format(prefix, msg))

def echo_msg2(msg, prefix = 'waffles', nl = True):
    '''echo `msg` to stderr using `prefix`
    >> echo_msg2('message', 'test')
    test: message'''
    
    sys.stderr.write('\x1b[2K\r')
    sys.stderr.flush()
    sys.stderr.write('{}: {}{}'.format(prefix, msg, '\n' if nl else ''))

## ==============================================
## echo message `m` to sys.stderr using
## auto-generated prefix
## lambda runs: echo_msg2(m, prefix = os.path.basename(sys.argv[0]))
## ==============================================
echo_msg = lambda m: echo_msg2(m, prefix = os.path.basename(sys.argv[0]))

## ==============================================
## echo error message `m` to sys.stderr using
## auto-generated prefix
## ==============================================
echo_error_msg = lambda m: echo_error_msg2(m, prefix = os.path.basename(sys.argv[0]))

class _progress:
    '''geomods minimal progress indicator'''

    def __init__(self, message = None):
        self.tw = 7
        self.count = 0
        self.pc = self.count % self.tw
        self.opm = message
        self.spinner = ['*     ', '**    ', '***   ', ' ***  ', '  *** ', '   ***', '    **', '     *']
        self.add_one = lambda x: x + 1
        self.sub_one = lambda x: x - 1
        self.spin_way = self.add_one

        if self.opm is not None:
            self._clear_stderr()
            sys.stderr.write('\r {}  {:40}\n'.format(" " * (self.tw - 1), self.opm))
        
    def _switch_way(self):
        self.spin_way = self.sub_one if self.spin_way == self.add_one else self.add_one

    def _clear_stderr(self, slen = 79):
        sys.stderr.write('\x1b[2K\r')
        sys.stderr.flush()

    def update(self):
        self.pc = (self.count % self.tw)
        self.sc = (self.count % (self.tw+1))
        self._clear_stderr()
        sys.stderr.write('\r[\033[36m{:6}\033[m] {:40}\r'.format(self.spinner[self.sc], self.opm))
        if self.count == self.tw: self.spin_way = self.sub_one
        if self.count == 0: self.spin_way = self.add_one
        self.count = self.spin_way(self.count)

    def end(self, status, end_msg = None):
        self._clear_stderr()
        if end_msg is None: end_msg = self.opm
        if status != 0:
            sys.stderr.write('\r[\033[31m\033[1m{:^6}\033[m] {:40}\n'.format('fail', end_msg))
        else: sys.stderr.write('\r[\033[32m\033[1m{:^6}\033[m] {:40}\n'.format('ok', end_msg))

## ==============================================
## regions - regions are a bounding box list:
## [w, e, s, n]
## -- regions.py --
## ==============================================
def region_valid_p(region):
    '''return True if `region` [xmin, xmax, ymin, ymax] appears to be valid'''
    
    if region is not None:
        if region[0] < region[1] and region[2] < region[3]: return(True)
        else: return(False)
    else: return(False)

def region_center(region):
    '''find the center point [xc, yc] of the `region` [xmin, xmax, ymin, ymax]

    returns the center point [xc, yc]'''
    
    xc = region[0] + (region[1] - region[0] / 2)
    yc = region[2] + (region[3] - region[2] / 2)
    return([xc, yc])

def region_pct(region, pctv):
    '''calculate a percentage buffer for the `region` [xmin, xmax, ymin, ymax]

    returns the pctv buffer val of the region'''
    
    ewp = (region[1] - region[0]) * (pctv * .01)
    nsp = (region[3] - region[2]) * (pctv * .01)
    return((ewp + nsp) / 2)

def region_buffer(region, bv = 0, pct = False):
    '''return the region buffered by buffer-value `bv`
    if `pct` is True, attain the buffer-value via: region_pct(region, bv)

    returns the buffered region [xmin, xmax, ymin, ymax]'''
    
    if pct: bv = region_pct(region, bv)
    return([region[0] - bv, region[1] + bv, region[2] - bv, region[3] + bv])

def regions_reduce(region_a, region_b):
    '''combine two regions and find their minimum combined region.
    if the regions don't overlap, will return an invalid region.
    check the result with region_valid_p()
    
    return the minimum region [xmin, xmax, ymin, ymax] when combining `region_a` and `region_b`'''
    
    region_c = [0, 0, 0, 0]
    region_c[0] = region_a[0] if region_a[0] > region_b[0] else region_b[0]
    region_c[1] = region_a[1] if region_a[1] < region_b[1] else region_b[1]
    region_c[2] = region_a[2] if region_a[2] > region_b[2] else region_b[2]
    region_c[3] = region_a[3] if region_a[3] < region_b[3] else region_b[3]
    return(region_c)

def regions_merge(region_a, region_b):
    '''combine two regions and find their maximum combined region.

    returns maximum region [xmin, xmax, ymin, ymax] when combining `region_a` `and region_b`'''
    
    region_c = [0, 0, 0, 0]
    region_c[0] = region_a[0] if region_a[0] < region_b[0] else region_b[0]
    region_c[1] = region_a[1] if region_a[1] > region_b[1] else region_b[1]
    region_c[2] = region_a[2] if region_a[2] < region_b[2] else region_b[2]
    region_c[3] = region_a[3] if region_a[3] > region_b[3] else region_b[3]
    if len(region_a) > 4 and len(region_b) > 4:
        region_c.append(region_a[4] if region_a[4] < region_b[4] else region_b[4])
        region_c.append(region_a[5] if region_a[5] > region_b[5] else region_b[5])
    return(region_c)

def regions_intersect_p(region_a, region_b):
    '''check if two regions intersect.
    region_valid_p(regions_reduce(region_a, region_b))

    return True if `region_a` and `region_b` intersect else False'''
    
    if region_a is not None and region_b is not None:
        return(region_valid_p(regions_reduce(region_a, region_b)))
    else: return(False)
    
def regions_intersect_ogr_p(region_a, region_b):
    '''check if two regions intersect.
    region_a_ogr_geom.Intersects(region_b_ogr_geom)
    
    return True if `region_a` and `region_b` intersect else False.'''
    
    if region_a is not None and region_b is not None:
        geom_a = gdal_region2geom(region_a)
        geom_b = gdal_region2geom(region_b)
        if geom_a.Intersects(geom_b):
            return(True)
        else: return(False)
    else: return(True)

def region_format(region, t = 'gmt'):
    '''format region to string, defined by `t`
    t = 'str': xmin/xmax/ymin/ymax
    t = 'gmt': -Rxmin/xmax/ymin/ymax
    t = 'bbox': xmin,ymin,xmax,ymax
    t = 'te': xmin ymin xmax ymax
    t = 'ul_lr': xmin ymax xmax ymin
    t = 'fn': ymax_xmin

    returns the formatted region as str'''

    if t == 'str': return('/'.join([str(x) for x in region[:4]]))
    elif t == 'gmt': return('-R' + '/'.join([str(x) for x in region[:4]]))
    elif t == 'bbox': return(','.join([str(region[0]), str(region[2]), str(region[1]), str(region[3])]))
    elif t == 'te': return(' '.join([str(region[0]), str(region[2]), str(region[1]), str(region[3])]))
    elif t == 'ul_lr': return(' '.join([str(region[0]), str(region[3]), str(region[1]), str(region[2])]))
    elif t == 'fn':
        ns = 's' if region[3] < 0 else 'n'
        ew = 'e' if region[0] > 0 else 'w'
        return('{}{:02d}x{:02d}_{}{:03d}x{:02d}'.format(ns, abs(int(region[3])), abs(int(region[3] * 100)) % 100, 
                                                        ew, abs(int(region[0])), abs(int(region[0] * 100)) % 100))
    elif t == 'inf': return(' '.join([str(x) for x in region]))

def region_chunk(region, inc, n_chunk = 10):
    '''chunk the region [xmin, xmax, ymin, ymax] into 
    n_chunk by n_chunk cell regions, given inc.

    returns a list of chunked regions.'''
    
    i_chunk = 0
    x_i_chunk = 0
    x_chunk = n_chunk
    o_chunks = []
    xcount, ycount, dst_gt = gdal_region2gt(region, inc)
    
    while True:
        y_chunk = n_chunk
        while True:
            this_x_origin = x_chunk - n_chunk
            this_y_origin = y_chunk - n_chunk
            this_x_size = x_chunk - this_x_origin
            this_y_size = y_chunk - this_y_origin
            
            geo_x_o = region[0] + this_x_origin * inc
            geo_x_t = geo_x_o + this_x_size * inc
            geo_y_o = region[2] + this_y_origin * inc
            geo_y_t = geo_y_o + this_y_size * inc

            if geo_y_t > region[3]: geo_y_t = region[3]
            if geo_y_o < region[2]: geo_y_o = region[2]
            if geo_x_t > region[1]: geo_x_t = region[1]
            if geo_x_o < region[0]: geo_x_o = region[0]
            o_chunks.append([geo_x_o, geo_x_t, geo_y_o, geo_y_t])
        
            if y_chunk < ycount:
                y_chunk += n_chunk
                i_chunk += 1
            else: break
        if x_chunk < xcount:
            x_chunk += n_chunk
            x_i_chunk += 1
        else: break
    return(o_chunks)

def regions_sort(trainers):
    '''sort regions by distance; regions is a list of regions [xmin, xmax, ymin, ymax].

    returns the sorted region-list'''
    
    train_sorted = []
    for z, train in enumerate(trainers):
        train_d = []
        np.random.shuffle(train)
        while True:
            if len(train) == 0: break
            this_center = region_center(train[0][0])
            train_d.append(train[0])
            train = train[1:]
            if len(train) == 0: break
            dsts = [hav_dst(this_center, region_center(x[0])) for x in train]
            min_dst = np.percentile(dsts, 50)
            d_t = lambda t: hav_dst(this_center, region_center(t[0])) > min_dst
            np.random.shuffle(train)
            train.sort(reverse=True, key=d_t)
        #echo_msg(' '.join([region_format(x[0], 'gmt') for x in train_d[:25]]))
        train_sorted.append(train_d)
    return(train_sorted)

def region_warp(region, s_warp = 4326, t_warp = 4326):
    src_srs = osr.SpatialReference()
    src_srs.ImportFromEPSG(int(s_warp))

    if t_warp is not None:
        dst_srs = osr.SpatialReference()
        dst_srs.ImportFromEPSG(int(t_warp))
        dst_trans = osr.CoordinateTransformation(src_srs, dst_srs)        
        pointA = ogr.CreateGeometryFromWkt('POINT ({} {})'.format(region[0], region[2]))
        pointB = ogr.CreateGeometryFromWkt('POINT ({} {})'.format(region[1], region[3]))
        pointA.Transform(dst_trans)
        pointB.Transform(dst_trans)
        region = [pointA.GetX(), pointB.GetX(), pointA.GetY(), pointB.GetY()]
    return(region)

def z_region_pass(region, upper_limit = None, lower_limit = None):
    if region is not None:
        z_region = region[4:]
        if z_region is not None and len(z_region) >= 2:
            if upper_limit is not None:
                if z_region[0] > upper_limit:
                    return(False)
            if lower_limit is not None:
                if z_region[1] < lower_limit:
                    return(False)
    return(True)

def z_pass(z, upper_limit = None, lower_limit = None):
    if upper_limit is not None:
        if z > upper_limit:
            return(False)
    if lower_limit is not None:
        if z < lower_limit:
            return(False)
    return(True)

## =============================================================================
##
## VDatum - vdatumfun.py
## wrapper functions for NOAA's VDatum
##
## Currently only compatible with VDatum >= 4.0
##
## TODO: add all vdatum cli options
## =============================================================================
_vd_config = {
    'jar': None,
    'ivert': 'navd88:m:height',
    'overt': 'mhw:m:height',
    'ihorz': 'NAD83_2011',
    'ohorz': 'NAD83_2011',
    'region': '3',
    'fmt': 'txt',
    'xyzl': '0,1,2',
    'skip': '0',
    'delim': 'space',
    'result_dir': 'result',
    'verbose': False,
}

def vdatum_locate_jar():
    '''Find the VDatum executable on the local system.

    returns a list of found vdatum.jar system paths'''
    
    results = []
    for root, dirs, files in os.walk('/'):
        if 'vdatum.jar' in files:
            results.append(os.path.abspath(os.path.join(root, 'vdatum.jar')))
            break
    if len(results) == 0:
        return(None)
    else: return(results)

def vdatum_get_version(vd_config = _vd_config):
    '''run vdatum and attempt to get it's version
    
    return the vdatum version or None'''
    
    if vd_config['jar'] is None:
        vd_config['jar'] = vdatum_locate_jar()
    if vd_config['jar'] is not None:
        out, status = run_cmd('java -jar {} {}'.format(vd_config['jar'], '-'), verbose = self.verbose)
        for i in out.decode('utf-8').split('\n'):
            if '- v' in i.strip():
                return(i.strip().split('v')[-1])
    return(None)

def vdatum_xyz(xyz, vd_config = _vd_config):
    if vd_config['jar'] is None: vd_config['jar'] = vdatum_locate_jar()[0]
    if vd_config['jar'] is not None:
        vdc = 'ihorz:{} ivert:{} ohorz:{} overt:{} -nodata -pt:{},{},{} region:{}\
        '.format(vd_config['ihorz'], vd_config['ivert'], vd_config['ohorz'], vd_config['overt'], \
                 xyz[0], xyz[1], xyz[2], vd_config['region'])
        out, status = run_cmd('java -Djava.awt.headless=false -jar {} {}'.format(vd_config['jar'], vdc), verbose = False)
        for i in out.split('\n'):
            if 'Height/Z' in i:
                z = float(i.split()[2])
                break
        return([xyz[0], xyz[1], z])
    else: return(xyz)

def vdatum_clean_result(result_f = 'result'):
    remove_glob('{}/*'.format(result_f))
    try:
        os.removedirs(result_f)
    except: pass
    
def run_vdatum(src_fn, vd_config = _vd_config):
    '''run vdatum on src_fn which is an XYZ file
    use vd_config to set vdatum parameters.

    returns [command-output, command-return-code]'''
    
    if vd_config['jar'] is None: vd_config['jar'] = vdatum_locate_jar()[0]
    if vd_config['jar'] is not None:
        vdc = 'ihorz:{} ivert:{} ohorz:{} overt:{} -nodata -file:txt:{},{},skip{}:{}:{} region:{}\
        '.format(vd_config['ihorz'], vd_config['ivert'], vd_config['ohorz'], vd_config['overt'], \
                 vd_config['delim'], vd_config['xyzl'], vd_config['skip'], src_fn, vd_config['result_dir'], vd_config['region'])
        #return(run_cmd('java -jar {} {}'.format(vd_config['jar'], vdc), verbose = True))
        return(run_cmd('java -Djava.awt.headless=true -jar {} {}'.format(vd_config['jar'], vdc), verbose = vd_config['verbose']))
    else: return([], -1)
    
## ==============================================
## GMT Wrapper Functions - gmtfun.py
## wrapper functions to GMT system commands
##
## GMT must be installed on the system to run these
## functions and commands.
## ==============================================
def gmt_inf(src_xyz):
    '''generate an info (.inf) file from a src_xyz file using GMT.

    returns [cmd-output, cmd-return-code]'''
    
    return(run_cmd('gmt gmtinfo {} -C > {}.inf'.format(src_xyz, src_xyz), verbose = False))

def gmt_grd_inf(src_grd):
    '''generate an info (.inf) file from a src_gdal file using GMT.

    returns [cmd-output, cmd-return-code]'''
    
    return(run_cmd('gmt grdinfo {} -C > {}.inf'.format(src_grd, src_grd), verbose = False))

def gmt_inc2inc(inc_str):
    '''convert a GMT-style `inc_str` (6s) to geographic units
    c/s - arc-seconds
    m - arc-minutes

    return float increment value.'''
    
    if inc_str is None or inc_str.lower() == 'none': return(None)
    units = inc_str[-1]
    if units == 'c': inc = float(inc_str[:-1]) / 3600
    elif units == 's': inc = float(inc_str[:-1]) / 3600
    elif units == 'm': inc = float(inc_str[:-1]) / 360
    else:
        try:
            inc = float(inc_str)
        except ValueError as e:
            echo_error_msg('could not parse increment {}, {}'.format(inc_str, e))
            return(None)
    return(inc)

def gmt_grd2gdal(src_grd, dst_fmt = 'GTiff', epsg = 4326, verbose = False):
    '''convert the grd file to tif using GMT

    returns the gdal file name or None'''
    
    dst_gdal = '{}.{}'.format(os.path.basename(src_grd).split('.')[0], gdal_fext(dst_fmt))
    grd2gdal_cmd = ('gmt grdconvert {} {}=gd+n-9999:{} -V\
    '.format(src_grd, dst_gdal, dst_fmt))
    out, status = run_cmd(grd2gdal_cmd, verbose = verbose)
    if status == 0:
        return(dst_gdal)
    else: return(None)

def gmt_grdinfo(src_grd, verbose = False):
    '''gather infos about src_grd using GMT grdinfo.

    return an info list of `src_grd`'''
    
    out, status = run_cmd('gmt gmtset IO_COL_SEPARATOR = SPACE', verbose = verbose)
    grdinfo_cmd = ('gmt grdinfo {} -C'.format(src_grd))
    out, status = run_cmd(grdinfo_cmd, verbose = verbose)
    remove_glob('gmt.conf')
    if status == 0:
        return(out.split())
    else: return(None)

def gmt_gmtinfo(src_xyz, verbose = False):
    '''gather infos about src_xyz using GMT gmtinfo

    return an info list of `src_xyz`'''
    
    out, status = run_cmd('gmt gmtset IO_COL_SEPARATOR = SPACE', verbose = verbose)
    gmtinfo_cmd = ('gmt gmtinfo {} -C'.format(src_xyz))
    out, status = run_cmd(gmtinfo_cmd, verbose = verbose)
    remove_glob('gmt.conf')
    if status == 0:
        return(out.split())
    else: return(None)
        
def gmt_select_split(o_xyz, sub_region, sub_bn, verbose = False):
    '''split an xyz file into an inner and outer region.

    returns [inner_region, outer_region]'''
    
    out_inner = None
    out_outer = None
    gmt_s_inner = 'gmt gmtselect -V {} {} > {}_inner.xyz'.format(o_xyz, region_format(sub_region, 'gmt'), sub_bn)
    out, status = run_cmd(gmt_s_inner, verbose = verbose)
    if status == 0: out_inner = '{}_inner.xyz'.format(sub_bn)
    gmt_s_outer = 'gmt gmtselect -V {} {} -Ir > {}_outer.xyz'.format(o_xyz, region_format(sub_region, 'gmt'), sub_bn)
    out, status = run_cmd(gmt_s_outer, verbose = verbose)
    if status == 0:  out_outer = '{}_outer.xyz'.format(sub_bn)
    return([out_inner, out_outer])
        
def gmt_grdcut(src_grd, src_region, dst_grd, verbose = False):
    '''cut `src_grd` to `src_region` using GMT grdcut
    
    returns [cmd-output, cmd-return-code]'''
    
    cut_cmd1 = ('gmt grdcut -V {} -G{} {}'.format(src_grd, dst_grd, src_region.gmt))
    return(run_cmd(cut_cmd1, verbose = True))

def gmt_grdfilter(src_grd, dst_grd, dist = '3s', verbose = False):
    '''filter `src_grd` using GMT grdfilter

    returns [cmd-output, cmd-return-code]'''
    
    ft_cmd1 = ('gmt grdfilter -V {} -G{} -R{} -Fc{} -D1'.format(src_grd, dst_grd, src_grd, dist))
    return(run_cmd(ft_cmd1, verbose = verbose))

def gmt_nan2zero(src_grd, node = 'pixel', verbose = False):
    '''convert nan values in `src_grd` to zero

    returns status code (0 == success) '''
    
    num_msk_cmd = ('gmt grdmath -V {} 0 MUL 1 ADD 0 AND = tmp.tif=gd+n-9999:GTiff'.format(src_grd))
    out, status = run_cmd(num_msk_cmd, verbose = True)
    if status == 0: os.rename('tmp.tif', '{}'.format(src_grd))
    return(status)

def gmt_grdcut(src_grd, region, verbose = False):
    '''cut a grid to region using GMT grdcut

    return status code (0 == success)'''
    
    cut_cmd = ('gmt grdcut -V {} -Gtmp.grd {}\
    '.format(src_grd, region_format(region, 'gmt')))
    out, status = run_cmd(cut_cmd, verbose = True)
    if status == 0:
        remove_glob(src_grd)
        os.rename('tmp.grd', '{}'.format(src_grd))
    return(status)

def gmt_slope(src_dem, dst_slp, verbose = False):
    '''generate a Slope grid from a DEM with GMT

    return status code (0 == success)'''
    
    o_b_name = '{}'.format(src_dem.split('.')[0])
    slope_cmd0 = ('gmt grdgradient -V -fg {} -S{}_pslp.grd -D -R{}\
    '.format(src_dem, o_b_name, src_dem))
    out, status = run_cmd(slope_cmd0, verbose = verbose)
    if status == 0:
        slope_cmd1 = ('gmt grdmath -V {}_pslp.grd ATAN PI DIV 180 MUL = {}=gd+n-9999:GTiff\
        '.format(o_b_name, dst_slp))
        out, status = run_cmd(slope_cmd1, verbose = verbose)
    remove_glob('{}_pslp.grd'.format(o_b_name))
    return(status)

def gmt_num_msk(num_grd, dst_msk, verbose = False):
    '''generate a num-msk from a NUM grid using GMT grdmath

    returns [cmd-output, cmd-return-code]'''
    
    num_msk_cmd = ('gmt grdmath -V {} 0 MUL 1 ADD 0 AND = {}\
    '.format(num_grd, dst_msk))
    return(run_cmd(num_msk_cmd, verbose = verbose))

def gmt_sample_gnr(src_grd, verbose = False):
    '''resamele src_grd to toggle between grid-node and pixel-node
    grid registration.

    returns status code (0 == success)'''
    
    out, status = run_cmd('gmt grdsample -T {} -Gtmp.tif=gd+n-9999:GTiff'.format(src_grd), verbose = verbose)
    if status == 0: os.rename('tmp.tif', '{}'.format(src_grd))
    return(status)

def gmt_sample_inc(src_grd, inc = 1, verbose = False):
    '''resamele src_grd to increment `inc` using GMT grdsample

    returns status code (0 == success)'''
    
    out, status = run_cmd('gmt grdsample -I{:.10f} {} -R{} -Gtmp.tif=gd+n-9999:GTiff'.format(inc, src_grd, src_grd), verbose = verbose)
    if status == 0: os.rename('tmp.tif', '{}'.format(src_grd))
    return(status)

## ==============================================
## MB-System Wrapper Functions - mbsfun.py
##
## MS-System must be installed on the system to run
## these functions and commands.
## ==============================================
def mb_inf(src_xyz, src_fmt = 168):
    '''generate an info (.inf) file from a src_xyz file using MBSystem.

    return inf_parse(inf_file)'''
    
    run_cmd('mbdatalist -O -F{} -I{}'.format(src_fmt, src_xyz))
    return(inf_parse('{}.inf'.format(src_xyz)))

## ==============================================
## GDAL Wrappers and Functions - gdalfun.py
## ==============================================
gdal.PushErrorHandler('CPLQuietErrorHandler')
gdal.UseExceptions()
_gdal_progress = gdal.TermProgress #crashes on osgeo4w
_gdal_progress_nocb = gdal.TermProgress_nocb
    
def gdal_sr_wkt(epsg, esri = False):
    '''convert an epsg code to wkt

    returns the sr Wkt or None'''
    
    try:
        int(epsg)
        sr = osr.SpatialReference()
        sr.ImportFromEPSG(epsg)
        if esri: sr.MorphToESRI()
        return(sr.ExportToWkt())
    except: return(None)

def gdal_fext(src_drv_name):
    '''find the common file extention given a GDAL driver name
    older versions of gdal can't do this, so fallback to known standards.

    returns list of known file extentions or None'''
    
    fexts = None
    try:
        drv = gdal.GetDriverByName(src_drv_name)
        if drv.GetMetadataItem(gdal.DCAP_RASTER): fexts = drv.GetMetadataItem(gdal.DMD_EXTENSIONS)
        if fexts is not None: return(fexts.split()[0])
        else: return(None)
    except:
        if src_drv_name == 'GTiff': fext = 'tif'
        elif src_drv_name == 'HFA': fext = 'img'
        elif src_drv_name == 'GMT': fext = 'grd'
        elif src_drv_name.lower() == 'netcdf': fext = 'nc'
        else: fext = 'gdal'
        return(fext)

def gdal_prj_file(dst_fn, epsg):
    '''generate a .prj file given an epsg code

    returns 0'''
    
    with open(dst_fn, 'w') as out:
        out.write(gdal_sr_wkt(int(epsg), True))
    return(0)
    
def gdal_set_epsg(src_fn, epsg = 4326):
    '''set the projection of gdal file src_fn to epsg

    returns status-code (0 == success)'''
    
    ds = gdal.Open(src_fn, gdal.GA_Update)
    if ds is not None:
        ds.SetProjection(gdal_sr_wkt(int(epsg)))
        ds = None
        return(0)
    else: return(None)

def gdal_set_nodata(src_fn, nodata = -9999):
    '''set the nodata value of gdal file src_fn

    returns 0'''
    
    ds = gdal.Open(src_fn, gdal.GA_Update)
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(float(nodata))
    ds = None
    return(0)

def gdal_infos(src_fn, scan = False):
    '''scan gdal file src_fn and gather region info.

    returns region dict.'''
    
    if os.path.exists(src_fn):
        ds = gdal.Open(src_fn)
        if ds is not None:
            dsc = gdal_gather_infos(ds, scan = scan)
            ds = None
            return(dsc)
        else: return(None)
    else: return(None)

def gdal_gather_infos(src_ds, scan = False):
    '''gather information from `src_ds` GDAL dataset

    returns gdal_config dict.'''

    src_band = src_ds.GetRasterBand(1)
    
    ds_config = {
        'nx': src_ds.RasterXSize,
        'ny': src_ds.RasterYSize,
        'nb':src_ds.RasterCount,
        'geoT': src_ds.GetGeoTransform(),
        'proj': src_ds.GetProjectionRef(),
        'dt': src_band.DataType,
        'dtn': gdal.GetDataTypeName(src_band.DataType),
        'ndv': src_band.GetNoDataValue(),
        'fmt': src_ds.GetDriver().ShortName,
        'zr': None,
    }
    if ds_config['ndv'] is None: ds_config['ndv'] = -9999
    if scan:
        try:
            ds_config['zr'] = src_band.ComputeRasterMinMax()
        except: ds_config = None
    
    return(ds_config)

def gdal_set_infos(nx, ny, nb, geoT, proj, dt, ndv, fmt):
    '''set a datasource config dictionary

    returns gdal_config dict.'''
    
    return({'nx': nx, 'ny': ny, 'nb': nb, 'geoT': geoT, 'proj': proj, 'dt': dt, 'ndv': ndv, 'fmt': fmt})

def gdal_cpy_infos(src_config):
    '''copy src_config

    returns copied src_config dict.'''
    
    dst_config = {}
    for dsc in src_config.keys():
        dst_config[dsc] = src_config[dsc]
    return(dst_config)

def gdal_write (src_arr, dst_gdal, ds_config, dst_fmt = 'GTiff'):
    '''write src_arr to gdal file dst_gdal using src_config

    returns [output-gdal, status-code]'''
    
    driver = gdal.GetDriverByName(dst_fmt)
    if os.path.exists(dst_gdal): driver.Delete(dst_gdal)
    ds = driver.Create(dst_gdal, ds_config['nx'], ds_config['ny'], 1, ds_config['dt'])
    if ds is not None:
        ds.SetGeoTransform(ds_config['geoT'])
        ds.SetProjection(ds_config['proj'])
        ds.GetRasterBand(1).SetNoDataValue(ds_config['ndv'])
        ds.GetRasterBand(1).WriteArray(src_arr)
        ds = None
        return(dst_gdal, 0)
    else: return(None, -1)

def gdal_null(dst_fn, region, inc, nodata = -9999, outformat = 'GTiff'):
    '''generate a `null` grid with gdal

    returns [output-gdal, status-code]'''
    
    xcount, ycount, dst_gt = gdal_region2gt(region, inc)
    null_array = np.zeros((ycount, xcount))
    null_array[null_array == 0] = nodata
    ds_config = gdal_set_infos(xcount, ycount, xcount * ycount, dst_gt, gdal_sr_wkt(4326), gdal.GDT_Float32, -9999, outformat)
    return(gdal_write(null_array, dst_fn, ds_config))
    
def gdal_cut(src_gdal, region, dst_fn):
    '''cut src_fn gdal file to srcwin and output dst_fn gdal file

    returns [output-gdal, status-code]'''
    
    src_ds = gdal.Open(src_gdal)
    if src_ds is not None:
        ds_config = gdal_gather_infos(src_ds)
        srcwin = gdal_srcwin(src_ds, region)
        gt = ds_config['geoT']
        ds_arr = src_ds.GetRasterBand(1).ReadAsArray(srcwin[0], srcwin[1], srcwin[2], srcwin[3])
        dst_gt = (gt[0] + (srcwin[0] * gt[1]), gt[1], 0., gt[3] + (srcwin[1] * gt[5]), 0., gt[5])
        ds_config = gdal_set_infos(srcwin[2], srcwin[3], srcwin[2] * srcwin[3], dst_gt, gdal_sr_wkt(4326), ds_config['dt'], ds_config['ndv'], ds_config['fmt'])
        src_ds = None
        return(gdal_write(ds_arr, dst_fn, ds_config))
    else: return(None, -1)

def gdal_clip(src_gdal, src_ply = None, invert = False):
    '''clip dem to polygon `src_ply`, optionally invert the clip.

    returns [gdal_raserize-output, gdal_rasterize-return-code]'''
    
    gi = gdal_infos(src_gdal)
    if gi is not None and src_ply is not None:
        gr_inv = '-i' if invert else ''
        gr_cmd = 'gdal_rasterize -burn {} {} -l {} {} {}'\
                 .format(gi['ndv'], gr_inv, os.path.basename(src_ply).split('.')[0], src_ply, src_gdal)
        return(run_cmd(gr_cmd, verbose = True))
    else: return(None)

def gdal_query(src_xyz, src_grd, out_form):
    '''query a gdal-compatible grid file with xyz data.
    out_form dictates return values

    returns array of values'''
    
    xyzl = []
    out_array = []

    ## ==============================================
    ## Process the src grid file
    ## ==============================================
    ds = gdal.Open(src_grd)
    if ds is not None:
        ds_config = gdal_gather_infos(ds)
        ds_band = ds.GetRasterBand(1)
        ds_gt = ds_config['geoT']
        ds_nd = ds_config['ndv']
        tgrid = ds_band.ReadAsArray()

        ## ==============================================   
        ## Process the src xyz data
        ## ==============================================
        for xyz in src_xyz:
            x = xyz[0]
            y = xyz[1]
            try: 
                z = xyz[2]
            except: z = ds_nd

            if x > ds_gt[0] and y < float(ds_gt[3]):
                xpos, ypos = _geo2pixel(x, y, ds_gt)
                try: 
                    g = tgrid[ypos, xpos]
                except: g = ds_nd
                #print(g)
                d = c = m = s = ds_nd
                if g != ds_nd:
                    d = z - g
                    m = z + g
                    outs = []
                    for i in out_form:
                        outs.append(vars()[i])
                    xyzl.append(np.array(outs))
        dsband = ds = None
        out_array = np.array(xyzl)
    return(out_array)

def gdal_yield_query(src_xyz, src_grd, out_form):
    '''query a gdal-compatible grid file with xyz data.
    out_form dictates return values

    yields out_form results'''
    
    xyzl = []
    out_array = []

    ## ==============================================
    ## Process the src grid file
    ## ==============================================
    ds = gdal.Open(src_grd)
    if ds is not None:
        ds_config = gdal_gather_infos(ds)
        ds_band = ds.GetRasterBand(1)
        ds_gt = ds_config['geoT']
        ds_nd = ds_config['ndv']
        tgrid = ds_band.ReadAsArray()
        dsband = ds = None
        
        ## ==============================================   
        ## Process the src xyz data
        ## ==============================================
        for xyz in src_xyz:
            x = xyz[0]
            y = xyz[1]
            try: 
                z = xyz[2]
            except: z = ds_nd

            if x > ds_gt[0] and y < float(ds_gt[3]):
                xpos, ypos = _geo2pixel(x, y, ds_gt)
                try: 
                    g = tgrid[ypos, xpos]
                except: g = ds_nd
                d = c = m = s = ds_nd
                if g != ds_nd:
                    d = z - g
                    m = z + g
                    outs = []
                    for i in out_form:
                        outs.append(vars()[i])
                    yield(outs)
    
def np_split(src_arr, sv = 0, nd = -9999):
    '''split numpy `src_arr` by `sv` (turn u/l into `nd`)

    returns [upper_array, lower_array]'''
    
    try:
        sv = int(sv)
    except: sv = 0
    u_arr = np.array(src_arr)
    l_arr = np.array(src_arr)
    u_arr[u_arr <= sv] = nd
    l_arr[l_arr >= sv] = nd
    return(u_arr, l_arr)

def gdal_split(src_gdal, split_value = 0):
    '''split raster file `src_gdal`into two files based on z value

    returns [upper_grid-fn, lower_grid-fn]'''
    
    dst_upper = os.path.join(os.path.dirname(src_gdal), '{}_u.tif'.format(os.path.basename(src_gdal)[:-4]))
    dst_lower = os.path.join(os.path.dirname(src_gdal), '{}_l.tif'.format(os.path.basename(src_gdal)[:-4]))
    src_ds = gdal.Open(src_gdal)
    if src_ds is not None:
        src_config = gdal_gather_infos(src_ds)
        dst_config = gdal_cpy_infos(src_config)
        dst_config['fmt'] = 'GTiff'
        ds_arr = src_ds.GetRasterBand(1).ReadAsArray(0, 0, src_config['nx'], src_config['ny'])
        ua, la = np_split(ds_arr, split_value, src_config['ndv'])
        gdal_write(ua, dst_upper, dst_config)
        gdal_write(la, dst_lower, dst_config)
        ua = la = ds_arr = src_ds = None
        return([dst_upper, dst_lower])
    else: return(None)

def gdal_crop(src_ds):
    '''crop `src_ds` GDAL datasource by it's NoData value. 

    returns [cropped array, cropped_gdal_config].'''
    
    ds_config = gdal_gather_infos(src_ds)
    ds_arr = src_ds.GetRasterBand(1).ReadAsArray()

    src_arr[elev_array == ds_config['ndv']] = np.nan
    nans = np.isnan(src_arr)
    nancols = np.all(nans, axis=0)
    nanrows = np.all(nans, axis=1)

    firstcol = nancols.argmin()
    firstrow = nanrows.argmin()        
    lastcol = len(nancols) - nancols[::-1].argmin()
    lastrow = len(nanrows) - nanrows[::-1].argmin()

    dst_arr = src_arr[firstrow:lastrow,firstcol:lastcol]
    src_arr = None

    dst_arr[np.isnan(dst_arr)] = ds_config['nv']
    GeoT = ds_config['geoT']
    dst_x_origin = GeoT[0] + (GeoT[1] * firstcol)
    dst_y_origin = GeoT[3] + (GeoT[5] * firstrow)
    dst_geoT = [dst_x_origin, GeoT[1], 0.0, dst_y_origin, 0.0, GeoT[5]]
    ds_config['geoT'] = dst_geoT
    return(dst_arr, ds_config)
        
def gdal_gdal2gdal(src_grd, dst_fmt = 'GTiff', epsg = 4326):
    '''convert the gdal file to gdal using gdal

    return output-gdal-fn'''
    
    if os.path.exists(src_grd):
        dst_gdal = '{}.{}'.format(os.path.basename(src_grd).split('.')[0], gdal_fext(dst_fmt))
        gdal2gdal_cmd = ('gdal_translate {} {} -f {}\
        '.format(src_grd, dst_gdal, dst_fmt))
        out, status = run_cmd(gdal2gdal_cmd, verbose = True)
        if status == 0: return(dst_gdal)
        else: return(None)
    else: return(None)

def gdal_sum(src_gdal):
    '''sum the z vale of src_gdal

    return the sum'''
    
    ds = gdal.Open(src_gdal)
    if ds is not None:
        ds_array = ds.GetRasterBand(1).ReadAsArray() 
        sums = np.sum(ds_array)
        ds = ds_array = None
        return(sums)
    else: return(None)

def gdal_percentile(src_gdal, perc = 95):
    '''calculate the `perc` percentile of src_fn gdal file.

    return the calculated percentile'''
    
    ds = gdal.Open(src_gdal)
    if ds is not None:
        ds_array = np.array(ds.GetRasterBand(1).ReadAsArray())
        x_dim = ds_array.shape[0]
        ds_array_flat = ds_array.flatten()
        p = np.percentile(ds_array_flat, perc)
        percentile = 2 if p < 2 else p
        ds = ds_array = None
        return(percentile)
    else: return(None)

def gdal_mask_analysis(mask = None):
    '''mask is a GDAL mask grid of 0/1

    returns [sum, max, percentile]'''
    
    msk_sum = gdal_sum(mask)
    msk_gc = gdal_infos(mask)
    msk_max = float(msk_gc['nx'] * msk_gc['ny'])
    msk_perc = float((msk_sum / msk_max) * 100.)
    return(msk_sum, msk_max, msk_perc)
    
def gdal_proximity(src_fn, dst_fn):
    '''compute a proximity grid via GDAL

    return 0 if success else None'''
    
    prog_func = None
    src_ds = gdal.Open(src_fn)
    dst_ds = None
    if src_ds is not None:
        src_band = src_ds.GetRasterBand(1)
        ds_config = gdal_gather_infos(src_ds)
        if dst_ds is None:
            drv = gdal.GetDriverByName('GTiff')
            dst_ds = drv.Create(dst_fn, ds_config['nx'], ds_config['ny'], 1, ds_config['dt'], [])
        dst_ds.SetGeoTransform(ds_config['geoT'])
        dst_ds.SetProjection(ds_config['proj'])
        dst_band = dst_ds.GetRasterBand(1)
        dst_band.SetNoDataValue(ds_config['ndv'])
        gdal.ComputeProximity(src_band, dst_band, ['DISTUNITS=PIXEL'], callback = prog_func)
        dst_band = src_band = dst_ds = src_ds = None
        return(0)
    else: return(None)
    
def gdal_gt2region(ds_config):
    '''convert a gdal geo-tranform to a region [xmin, xmax, ymin, ymax] via a data-source config dict.

    returns region of gdal data-source'''
    
    geoT = ds_config['geoT']
    return([geoT[0], geoT[0] + geoT[1] * ds_config['nx'], geoT[3] + geoT[5] * ds_config['ny'], geoT[3]])

def gdal_region2gt(region, inc):
    '''return a count info and a gdal geotransform based on extent and cellsize

    returns a list [xcount, ycount, geot]'''

    dst_gt = (region[0], inc, 0, region[3], 0, (inc * -1.))
    
    this_origin = _geo2pixel(region[0], region[3], dst_gt)
    this_end = _geo2pixel(region[1], region[2], dst_gt)
    #this_size = ((this_end[0] - this_origin[0]) + 1, (this_end[1] - this_origin[1]) + 1)
    this_size = (this_end[0] - this_origin[0], this_end[1] - this_origin[1])
    
    return(this_size[0], this_size[1], dst_gt)

def gdal_ogr_mask_union(src_layer, src_field, dst_defn = None):
    '''`union` a `src_layer`'s features based on `src_field` where
    `src_field` holds a value of 0 or 1. optionally, specify
    an output layer defn for the unioned feature.

    returns the output feature class'''
    
    if dst_defn is None: dst_defn = src_layer.GetLayerDefn()
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    feats = len(src_layer)
    echo_msg('unioning {} features'.format(feats))
    for n, f in enumerate(src_layer):
        _gdal_progress_nocb((n+1 / feats) * 100)
        if f.GetField(src_field) == 0:
            src_layer.DeleteFeature(f.GetFID())
        elif f.GetField(src_field) == 1:
            f.geometry().CloseRings()
            wkt = f.geometry().ExportToWkt()
            multi.AddGeometryDirectly(ogr.CreateGeometryFromWkt(wkt))
            src_layer.DeleteFeature(f.GetFID())
    #union = multi.UnionCascaded() ## slow on large multi...
    out_feat = ogr.Feature(dst_defn)
    out_feat.SetGeometry(multi)
    union = multi = None
    return(out_feat)

def gdal_ogr_regions(src_ds):
    '''return the region(s) of the ogr dataset'''
    
    these_regions = []
    if os.path.exists(src_ds):
        poly = ogr.Open(src_ds)
        if poly is not None:
            p_layer = poly.GetLayer(0)
            for pf in p_layer:
                pgeom = pf.GetGeometryRef()
                these_regions.append(pgeom.GetEnvelope())
        poly = None
    return(these_regions)

def gdal_create_polygon(coords):
    '''convert coords to Wkt

    returns polygon as wkt'''
    
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for coord in coords: ring.AddPoint(coord[1], coord[0])
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    poly_wkt = poly.ExportToWkt()
    poly = None
    return(poly_wkt)

def gdal_region2geom(region):
    '''convert an extent [west, east, south, north] to an OGR geometry

    returns ogr geometry'''
    
    eg = [[region[2], region[0]], [region[2], region[1]],
          [region[3], region[1]], [region[3], region[0]],
          [region[2], region[0]]]
    geom = ogr.CreateGeometryFromWkt(gdal_create_polygon(eg))
    return(geom)

def gdal_getEPSG(src_ds):
    '''returns the EPSG of the given gdal data-source'''
    
    ds_config = gdal_gather_infos(src_ds)
    ds_region = gdal_gt2region(ds_config)
    src_srs = osr.SpatialReference()
    src_srs.ImportFromWkt(ds_config['proj'])
    src_srs.AutoIdentifyEPSG()
    srs_auth = src_srs.GetAuthorityCode(None)

    return(srs_auth)

def gdal_region(src_ds, warp = None):
    '''return the extent of the src_fn gdal file.
    warp should be an epsg to warp the region to.

    returns the region of the gdal data-source'''
    
    ds_config = gdal_gather_infos(src_ds)
    ds_region = gdal_gt2region(ds_config)
    src_srs = osr.SpatialReference()
    src_srs.ImportFromWkt(ds_config['proj'])
    src_srs.AutoIdentifyEPSG()
    srs_auth = src_srs.GetAuthorityCode(None)
    
    if srs_auth is None or srs_auth == warp: warp = None

    if warp is not None:
        dst_srs = osr.SpatialReference()
        dst_srs.ImportFromEPSG(int(warp))
        #dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        dst_trans = osr.CoordinateTransformation(src_srs, dst_srs)

        pointA = ogr.CreateGeometryFromWkt('POINT ({} {})'.format(ds_region[0], ds_region[2]))
        pointB = ogr.CreateGeometryFromWkt('POINT ({} {})'.format(ds_region[1], ds_region[3]))
        pointA.Transform(dst_trans)
        pointB.Transform(dst_trans)
        ds_region = [pointA.GetX(), pointB.GetX(), pointA.GetY(), pointB.GetY()]
    return(ds_region)

def _geo2pixel(geo_x, geo_y, geoTransform):
    '''convert a geographic x,y value to a pixel location of geoTransform'''
    if geoTransform[2] + geoTransform[4] == 0:
        pixel_x = ((geo_x - geoTransform[0]) / geoTransform[1]) + .5
        pixel_y = ((geo_y - geoTransform[3]) / geoTransform[5]) + .5
    else: pixel_x, pixel_y = _apply_gt(geo_x, geo_y, _invert_gt(geoTransform))
    return(int(pixel_x), int(pixel_y))

def _pixel2geo(pixel_x, pixel_y, geoTransform):
    '''convert a pixel location to geographic coordinates given geoTransform'''
    
    geo_x, geo_y = _apply_gt(pixel_x, pixel_y, geoTransform)
    return(geo_x, geo_y)

def _apply_gt(in_x, in_y, geoTransform):
    '''apply geotransform to in_x,in_y'''
    
    out_x = geoTransform[0] + (in_x + 0.5) * geoTransform[1] + (in_y + 0.5) * geoTransform[2]
    out_y = geoTransform[3] + (in_x + 0.5) * geoTransform[4] + (in_y + 0.5) * geoTransform[5]
    return(out_x, out_y)

def _invert_gt(geoTransform):
    '''invert the geotransform'''
    
    det = geoTransform[1] * geoTransform[5] - geoTransform[2] * geoTransform[4]
    if abs(det) < 0.000000000000001: return
    invDet = 1.0 / det
    outGeoTransform = [0, 0, 0, 0, 0, 0]
    outGeoTransform[1] = geoTransform[5] * invDet
    outGeoTransform[4] = -geoTransform[4] * invDet
    outGeoTransform[2] = -geoTransform[2] * invDet
    outGeoTransfrom[5] = geoTransform[1] * invDet
    outGeoTransform[0] = (geoTransform[2] * geoTransform[3] - geoTransform[0] * geoTransform[5]) * invDet
    outGeoTransform[3] = (-geoTransform[1] * geoTransform[3] + geoTransform[0] * geoTransform[4]) * invDet
    return(outGeoTransform)

def gdal_srcwin(src_ds, region):
    '''given a gdal file src_fn and a region [w, e, s, n],
    output the appropriate gdal srcwin.

    returns the gdal srcwin'''
    
    ds_config = gdal_gather_infos(src_ds)
    this_origin = _geo2pixel(region[0], region[3], ds_config['geoT'])
    this_origin = [0 if x < 0 else x for x in this_origin]
    this_end = _geo2pixel(region[1], region[2], ds_config['geoT'])
    this_size = ((this_end[0] - this_origin[0]), (this_end[1] - this_origin[1]))
    this_size = [0 if x < 0 else x for x in this_size]
    if this_size[0] > ds_config['nx'] - this_origin[0]: this_size[0] = ds_config['nx'] - this_origin[0]
    if this_size[1] > ds_config['ny'] - this_origin[1]: this_size[1] = ds_config['ny'] - this_origin[1]
    return(this_origin[0], this_origin[1], this_size[0], this_size[1])

def xyz2gdal_ds(src_xyz, dst_ogr):
    '''Make a point vector OGR DataSet Object from src_xyz

    returns the in-memory GDAL data-source'''
    
    ds = gdal.GetDriverByName('Memory').Create('', 0, 0, 0, gdal.GDT_Unknown)
    layer = ds.CreateLayer(dst_ogr, geom_type = ogr.wkbPoint25D)
    fd = ogr.FieldDefn('long', ogr.OFTReal)
    fd.SetWidth(10)
    fd.SetPrecision(8)
    layer.CreateField(fd)
    fd = ogr.FieldDefn('lat', ogr.OFTReal)
    fd.SetWidth(10)
    fd.SetPrecision(8)
    layer.CreateField(fd)
    fd = ogr.FieldDefn('elev', ogr.OFTReal)
    fd.SetWidth(12)
    fd.SetPrecision(12)
    layer.CreateField(fd)
    f = ogr.Feature(feature_def = layer.GetLayerDefn())
    for this_xyz in src_xyz:
        f.SetField(0, this_xyz[0])
        f.SetField(1, this_xyz[1])
        f.SetField(2, this_xyz[2])
        wkt = 'POINT({:.8f} {:.8f} {:.10f})'.format(this_xyz[0], this_xyz[1], this_xyz[2])
        g = ogr.CreateGeometryFromWkt(wkt)
        f.SetGeometryDirectly(g)
        layer.CreateFeature(f)
    return(ds)

def gdal_xyz2gdal(src_xyz, dst_gdal, region, inc, dst_format = 'GTiff', mode = 'n', epsg = 4326, verbose = False):
    '''Create a GDAL supported grid from xyz data 
    `mode` of `n` generates a num grid
    `mode` of `m` generates a mean grid
    `mode` of `k` generates a mask grid

    returns output, status'''
    
    xcount, ycount, dst_gt = gdal_region2gt(region, inc)
    if verbose:
        echo_msg('gridding data with mode: {} to {}'.format(mode, dst_gdal))
        echo_msg('grid size: {}/{}'.format(ycount, xcount))
    if mode == 'm':
        sumArray = np.zeros((ycount, xcount))
    gdt = gdal.GDT_Float32
    #else: gdt = gdal.GDT_Int32
    ptArray = np.zeros((ycount, xcount))
    ds_config = gdal_set_infos(xcount, ycount, xcount * ycount, dst_gt, gdal_sr_wkt(epsg), gdt, -9999, dst_format)
    for this_xyz in src_xyz:
        x = this_xyz[0]
        y = this_xyz[1]
        z = this_xyz[2]
        if x > region[0] and x < region[1]:
            if y > region[2] and y < region[3]:
                xpos, ypos = _geo2pixel(x, y, dst_gt)
                try:
                    if mode == 'm':
                        sumArray[ypos, xpos] += z
                    if mode == 'n' or mode == 'm': ptArray[ypos, xpos] += 1
                    else: ptArray[ypos, xpos] = 1
                except: pass
    if mode == 'm':
        ptArray[ptArray == 0] = np.nan
        outarray = sumArray / ptArray
    elif mode == 'n': outarray = ptArray
    else: outarray = ptArray
    outarray[np.isnan(outarray)] = -9999
    return(gdal_write(outarray, dst_gdal, ds_config))

def gdal_xyz_mask(src_xyz, dst_gdal, region, inc, dst_format='GTiff', epsg = 4326):
    '''Create a num grid mask of xyz data. The output grid
    will contain 1 where data exists and 0 where no data exists.

    yields the xyz data'''
    
    xcount, ycount, dst_gt = gdal_region2gt(region, inc)
    ptArray = np.zeros((ycount, xcount))
    ds_config = gdal_set_infos(xcount, ycount, xcount * ycount, dst_gt, gdal_sr_wkt(epsg), gdal.GDT_Int32, -9999, 'GTiff')
    for this_xyz in src_xyz:
        yield(this_xyz)
        x = this_xyz[0]
        y = this_xyz[1]
        if x > region[0] and x < region[1]:
            if y > region[2] and y < region[3]:
                xpos, ypos = _geo2pixel(x, y, dst_gt)
                try:
                    ptArray[ypos, xpos] = 1
                except: pass
    out, status = gdal_write(ptArray, dst_gdal, ds_config)

def np_gaussian_blur(in_array, size):
    '''blur an array using fftconvolve from scipy.signal
    size is the blurring scale-factor.

    returns the blurred array'''
    
    from scipy.signal import fftconvolve
    from scipy.signal import convolve
    import scipy.fftpack._fftpack as sff
    padded_array = np.pad(in_array, size, 'symmetric')
    x, y = np.mgrid[-size:size + 1, -size:size + 1]
    g = np.exp(-(x**2 / float(size) + y**2 / float(size)))
    g = (g / g.sum()).astype(in_array.dtype)
    in_array = None
    #try:
    out_array = fftconvolve(padded_array, g, mode = 'valid')
    #except:
    #print('switching to convolve')
    #out_array = convolve(padded_array, g, mode = 'valid')
    return(out_array)

def gdal_blur(src_gdal, dst_gdal, sf = 1):
    '''gaussian blur on src_gdal using a smooth-factor of `sf`
    runs np_gaussian_blur(ds.Array, sf)'''
    
    ds = gdal.Open(src_gdal)
    if ds is not None:
        ds_config = gdal_gather_infos(ds)
        ds_array = ds.GetRasterBand(1).ReadAsArray(0, 0, ds_config['nx'], ds_config['ny'])
        ds = None
        msk_array = np.array(ds_array)
        msk_array[msk_array != ds_config['ndv']] = 1
        msk_array[msk_array == ds_config['ndv']] = np.nan
        ds_array[ds_array == ds_config['ndv']] = 0
        smooth_array = np_gaussian_blur(ds_array, int(sf))
        smooth_array = smooth_array * msk_array
        mask_array = ds_array = None
        smooth_array[np.isnan(smooth_array)] = ds_config['ndv']
        return(gdal_write(smooth_array, dst_gdal, ds_config))
    else: return([], -1)

def gdal_smooth(src_gdal, dst_gdal, fltr = 10, split_value = None, use_gmt = False):
    '''smooth `src_gdal` using smoothing factor `fltr`; optionally
    only smooth bathymetry (sub-zero)

    return 0 for success or -1 for failure'''
    
    if os.path.exists(src_gdal):
        if split_value is not None:
            dem_u, dem_l = gdal_split(src_gdal, split_value)
        else: dem_l = src_gdal
        if use_gmt:
            out, status = gmt_grdfilter(dem_l, 'tmp_fltr.tif=gd+n-9999:GTiff', dist = fltr, verbose = True)
        else: out, status = gdal_blur(dem_l, 'tmp_fltr.tif', fltr)
        if split_value is not None:
            ds = gdal.Open(src_gdal)
            ds_config = gdal_gather_infos(ds)
            msk_arr = ds.GetRasterBand(1).ReadAsArray()
            msk_arr[msk_arr != ds_config['ndv']] = 1
            msk_arr[msk_arr == ds_config['ndv']] = np.nan
            ds = None
            u_ds = gdal.Open(dem_u)
            if u_ds is not None:
                l_ds = gdal.Open('tmp_fltr.tif')
                if l_ds is not None:
                    u_arr = u_ds.GetRasterBand(1).ReadAsArray()
                    l_arr = l_ds.GetRasterBand(1).ReadAsArray()
                    u_arr[u_arr == ds_config['ndv']] = 0
                    l_arr[l_arr == ds_config['ndv']] = 0
                    ds_arr = (u_arr + l_arr) * msk_arr
                    ds_arr[np.isnan(ds_arr)] = ds_config['ndv']
                    gdal_write(ds_arr, 'merged.tif', ds_config)
                    l_ds = None
                    remove_glob(dem_l)
                u_ds = None
                remove_glob(dem_u)
            os.rename('merged.tif', 'tmp_fltr.tif')
        os.rename('tmp_fltr.tif', dst_gdal)
        return(0)
    else: return(-1)

def gdal_sample_inc(src_grd, inc = 1, verbose = False):
    '''resamele src_grd to toggle between grid-node and pixel-node grid registration.'''
    
    out, status = run_cmd('gdalwarp -tr {:.10f} {:.10f} {} -r bilinear -te -R{} -r -Gtmp.tif=gd+n-9999:GTiff'.format(inc, inc, src_grd, src_grd), verbose = verbose)
    if status == 0: os.rename('tmp.tif', '{}'.format(src_grd))
    return(status)
        
def gdal_polygonize(src_gdal, dst_layer, verbose = False):
    '''run gdal.Polygonize on src_ds and add polygon to dst_layer'''
    
    ds = gdal.Open('{}'.format(src_gdal))
    ds_arr = ds.GetRasterBand(1)
    if verbose: echo_msg('polygonizing {}'.format(src_gdal))
    gdal.Polygonize(ds_arr, None, dst_layer, 0, callback = _gdal_progress if verbose else None)
    ds = ds_arr = None
    return(0, 0)
    
def gdal_chunks(src_fn, n_chunk = 10):
    '''split `src_fn` GDAL file into chunks with `n_chunk` cells squared.

    returns a list of chunked filenames or None'''
    
    band_nums = []
    o_chunks = []
    if band_nums == []: band_nums = [1]
    i_chunk = 0
    x_i_chunk = 0
    x_chunk = n_chunk

    try:
        src_ds = gdal.Open(src_fn)
    except: src_ds = None
    if src_ds is not None:
        ds_config = gdal_gather_infos(src_ds)
        band = src_ds.GetRasterBand(1)
        gt = ds_config['geoT']

        while True:
            y_chunk = n_chunk
            while True:
                if x_chunk > ds_config['nx']:
                    this_x_chunk = ds_config['nx']
                else: this_x_chunk = x_chunk

                if y_chunk > ds_config['ny']:
                    this_y_chunk = ds_config['ny']
                else: this_y_chunk = y_chunk

                this_x_origin = x_chunk - n_chunk
                this_y_origin = y_chunk - n_chunk
                this_x_size = this_x_chunk - this_x_origin
                this_y_size = this_y_chunk - this_y_origin

                ## chunk size aligns with grid cellsize
                if this_x_size == 0 or this_y_size == 0: break
                
                srcwin = (this_x_origin, this_y_origin, this_x_size, this_y_size)
                this_geo_x_origin, this_geo_y_origin = _pixel2geo(this_x_origin, this_y_origin, gt)
                dst_gt = [this_geo_x_origin, float(gt[1]), 0.0, this_geo_y_origin, 0.0, float(gt[5])]
                
                band_data = band.ReadAsArray(srcwin[0], srcwin[1], srcwin[2], srcwin[3])
                if not np.all(band_data == band_data[0,:]):
                    o_chunk = '{}_chnk{}x{}.tif'.format(os.path.basename(src_fn).split('.')[0], x_i_chunk, i_chunk)
                    dst_fn = os.path.join(os.path.dirname(src_fn), o_chunk)
                    o_chunks.append(dst_fn)

                    dst_config = gdal_cpy_infos(ds_config)
                    dst_config['nx'] = this_x_size
                    dst_config['ny'] = this_y_size
                    dst_config['geoT'] = dst_gt                    
                    gdal_write(band_data, dst_fn, dst_config)

                band_data = None

                if y_chunk > ds_config['ny']:
                    break
                else: 
                    y_chunk += n_chunk
                    i_chunk += 1
            if x_chunk > ds_config['nx']:
                break
            else:
                x_chunk += n_chunk
                x_i_chunk += 1
        src_ds = None
        return(o_chunks)
    else: return(None)

def gdal_slope(src_gdal, dst_gdal):
    '''generate a slope grid with GDAL

    return cmd output and status'''
    
    gds_cmd = 'gdaldem slope {} {} -s 111120 -compute_edges'.format(src_gdal, dst_gdal)
    return(run_cmd(gds_cmd))
    
## ==============================================
## inf files (data info) inf.py
## mbsystem/gmt/waffles infos
## ==============================================
def inf_generate(data_path, data_fmt = 168):
    '''generate an info (.inf) file from the data_path'''
    
    return(inf_entry([data_path, data_fmt]), True)

def inf_parse(src_inf):
    '''parse an inf file (mbsystem or gmt)
    
    returns region: [xmin, xmax, ymin, ymax, zmin, zmax]'''
    
    minmax = [0, 0, 0, 0, 0, 0]
    with open(src_inf) as iob:
        for il in iob:
            til = il.split()
            if len(til) > 1:
                try: 
                    minmax = [float(x) for x in til]
                except:
                    if til[0] == 'Minimum':
                        if til[1] == 'Longitude:':
                            minmax[0] = til[2]
                            minmax[1] = til[5]
                        elif til[1] == 'Latitude:':
                            minmax[2] = til[2]
                            minmax[3] = til[5]
                        # elif til[1] == 'Altitude:':
                        #     minmax[4] = til[2]
                        #     minmax[5] = til[5]
                        elif til[1] == 'Depth:':
                            minmax[4] = float(til[5])*-1
                            minmax[5] = float(til[2])*-1
    #print(src_inf)
    #print(minmax)
    return([float(x) for x in minmax])

def inf_entry(src_entry, overwrite = False):
    '''Read .inf file and extract minmax info.
    the .inf file can either be an MBSystem style inf file or the 
    result of `gmt gmtinfo file.xyz -C`, which is a 6 column line 
    with minmax info, etc.

    returns the region of the inf file.'''
    
    minmax = None
    if os.path.exists(src_entry[0]):
        path_i = src_entry[0] + '.inf'
        if not os.path.exists(path_i) or overwrite:
            minmax = _dl_inf_h[src_entry[1]](src_entry)
        else: minmax = inf_parse(path_i)
    if not region_valid_p(minmax): minmax = None
    return(minmax)

## ==============================================
## gdal processing (datalist fmt:200)
## ==============================================
def gdal_parse(src_ds, dump_nodata = False, srcwin = None, mask = None, warp = None, verbose = False, z_region = None):
    '''send the data from gdal file src_gdal to dst_xyz port (first band only)
    optionally mask the output with `mask` or transform the coordinates to `warp` (epsg-code)'''

    #if verbose: sys.stderr.write('waffles: parsing gdal file {}...'.format(src_ds.GetDescription()))
    ln = 1
    band = src_ds.GetRasterBand(1)
    ds_config = gdal_gather_infos(src_ds)
    src_srs = osr.SpatialReference()
    src_srs.ImportFromWkt(ds_config['proj'])
    src_srs.AutoIdentifyEPSG()
    srs_auth = src_srs.GetAuthorityCode(None)
    
    if srs_auth is None or srs_auth == warp: warp = None

    if warp is not None:
        dst_srs = osr.SpatialReference()
        dst_srs.ImportFromEPSG(int(warp))
        ## GDAL 3+
        #dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        dst_trans = osr.CoordinateTransformation(src_srs, dst_srs)
        
    gt = ds_config['geoT']
    msk_band = None
    if mask is not None:
        src_mask = gdal.Open(mask)
        msk_band = src_mask.GetRasterBand(1)
    if srcwin is None: srcwin = (0, 0, ds_config['nx'], ds_config['ny'])
    nodata = ['{:g}'.format(-9999), 'nan', float('nan')]
    if band.GetNoDataValue() is not None: nodata.append('{:g}'.format(band.GetNoDataValue()))
    if dump_nodata: nodata = []
    for y in range(srcwin[1], srcwin[1] + srcwin[3], 1):
        band_data = band.ReadAsArray(srcwin[0], y, srcwin[2], 1)
        if z_region is not None:
            if z_region[0] is not None:
                band_data[band_data < z_region[0]] = -9999
            if z_region[1] is not None:
                band_data[band_data > z_region[1]] = -9999
        if msk_band is not None:
            msk_data = msk_band.ReadAsArray(srcwin[0], y, srcwin[2], 1)
            band_data[msk_data==0]=-9999
            #msk_data = None
        band_data = np.reshape(band_data, (srcwin[2], ))
        for x_i in range(0, srcwin[2], 1):
            x = x_i + srcwin[0]
            z = band_data[x_i]
            if '{:g}'.format(z) not in nodata:
                ln += 1
                geo_x,geo_y = _pixel2geo(x, y, gt)
                if warp is not None:
                    point = ogr.CreateGeometryFromWkt('POINT ({} {})'.format(geo_x, geo_y))
                    point.Transform(dst_trans)
                    pnt = point.GetPoint()
                    line = [pnt[0], pnt[1], z]
                else: line = [geo_x, geo_y, z]
                yield(line)
    band = None
    src_mask = None
    msk_band = None
    if verbose: echo_msg('parsed {} data records from {}'.format(ln, src_ds.GetDescription()))

def xyz_transform(src_fn, xyz_c = None, inc = None, vdatum = None, region = None, datalist = None, verbose = False):
    src_xyz, src_zips = procs_unzip(src_fn, _known_datalist_fmts[168])
    if xyz_c is None: xyz_c = copy.deepcopy(_xyz_config)
    if not os.path.exists(os.path.join(os.path.dirname(src_fn), 'xyz')): os.mkdir(os.path.join(os.path.dirname(src_fn), 'xyz'))

    if vdatum is not None:
        vds = vdatum.split(',')
        iv = vds[0]
        ov = vds[1]
        vdc = _vd_config
        if vdc['jar'] is None: vdc['jar'] = vdatum_locate_jar()[0]
        vdc['ivert'] = iv
        vdc['overt'] = ov
        vdc['delim'] = 'comma' if xyz_c['delim'] == ',' else 'space' if xyz_c['delim'] == ' ' else xyz_c['delim']
        vdc['skip'] = xyz_c['skip']
        vdc['xyzl'] = ','.join([str(x) for x in [xyz_c['xpos'], xyz_c['ypos'], xyz_c['zpos']]])
        out, status = run_vdatum(src_xyz, vdc)
        src_result = os.path.join('result', os.path.basename(src_xyz))
    else: src_result = src_xyz
    xyz_final = os.path.join('xyz', os.path.basename(src_xyz))

    if datalist is not None:
        datalist = os.path.join(os.path.dirname(src_fn), 'xyz', datalist)
    
    if os.path.exists(src_result):
        with open(src_result, 'r') as in_n, open(xyz_final, 'w') as out_n:
            for xyz in xyz_parse(in_n, xyz_c = xyz_c, region = region, verbose = verbose):
                xyz_line(xyz, out_n)

    if os.path.exists(xyz_final):
        if datalist is not None:
            mb_inf(xyz_final)
            datalist_append_entry([os.path.basename(xyz_final), 168, 1], datalist)
            if verbose: echo_msg('appended xyz file {} to datalist {}'.format(xyz_final, datalist))
                
    if src_xyz != src_fn: remove_glob(src_xyz)
    
def gdal2xyz_chunks(src_fn, chunk_value = 1000, inc  = None, epsg = None, vdatum = None, datalist = None, verbose = False):
    if verbose:
        echo_msg('------------------------------------')
        echo_msg('input gdal:\t\t{}'.format(src_fn))
        echo_msg('chunk size:\t\t{}'.format(chunk_value))
        echo_msg('output epsg:\t\t{}'.format(epsg))
        echo_msg('output increment:\t{}'.format(inc))
        echo_msg('vdatum string:\t{}'.format(vdatum))
        echo_msg('output datalist:\t{}'.format(datalist))
        echo_msg('------------------------------------')

    if not os.path.exists('xyz'): os.mkdir('xyz')

    src_gdal, src_zips = procs_unzip(src_fn, _known_datalist_fmts[200])
    src_c = gdal_infos(src_gdal)
    if verbose:
        echo_msg('{}'.format(src_c))
        echo_msg('chunking grid file {}...'.format(src_gdal))
    chunks = gdal_chunks(src_gdal, chunk_value)
    if verbose: echo_msg('generated {} chunks from {}'.format(len(chunks), src_gdal))
    if src_gdal != src_fn: remove_glob(src_gdal)

    if vdatum is not None:
        vds = vdatum.split(',')
        if len(vds) < 2:
            echo_error_msg('bad vdatum string {}'.format(vdatum))
            vdatum = None
        else:
            iv = vds[0]
            ov = vds[1]
            vdc = _vd_config
            if vdc['jar'] is None: vdc['jar'] = vdatum_locate_jar()[0]
            vdc['ivert'] = iv
            vdc['overt'] = ov
            vdc['delim'] = 'space'
            vdc['skip'] = '0'
            vdc['xyzl'] = '0,1,2'

    if datalist is not None:
        datalist = os.path.join('xyz', datalist)
        
    for i,chunk in enumerate(chunks):
        echo_msg('* processing chunk {} [{}/{}]...'.format(chunk, i+1, len(chunks)))
        xyz_chunk = '{}.xyz'.format(chunk.split('.')[0])
        xyz_chunk_final = os.path.join('xyz', os.path.basename(xyz_chunk))

        if epsg is not None or inc is not None:
            tif_chunk = '{}_warp.tif'.format(chunk.split('.')[0])
            gdw = 'gdalwarp {} -dstnodata -9999 -overwrite {}'.format(chunk, tif_chunk)
            if epsg is not None: gdw += ' -t_srs EPSG:{}'.format(epsg)
            if inc is not None: gdw += ' -tr {} {}'.format(inc, inc)
            out, status = run_cmd(gdw, verbose = verbose)
            remove_glob(chunk)
        else: tif_chunk = chunk

        with open(xyz_chunk, 'w') as xyz_c:
            gdal_dump_entry([tif_chunk, 200, None], dst_port = xyz_c, verbose = verbose)
        remove_glob(tif_chunk)

        if vdatum is not None:
            out, status = run_vdatum(xyz_chunk, vdc)
            remove_glob(xyz_chunk)
            xyz_chunk = os.path.join('result', os.path.basename(xyz_chunk)) 
            os.rename(xyz_chunk, xyz_chunk_final)
            vdatum_clean_result()

            if verbose: echo_msg('transformed {} chunk to {}'.format(iv, ov))
        else: os.rename(xyz_chunk, xyz_chunk_final)

        if datalist is not None:
            mb_inf(xyz_chunk_final)
            datalist_append_entry([os.path.basename(xyz_chunk_final), 168, 1], datalist)
            if verbose: echo_msg('appended xyz chunk {} to datalist {}'.format(xyz_chunk_final, datalist))
    
def gdal_inf(src_ds, warp = None):
    '''generate an info (.inf) file from a src_gdal file using gdal

    returns the region [xmin, xmax, ymin, ymax] of src_ds'''
    
    minmax = gdal_region(src_ds, warp)
    zr = src_ds.GetRasterBand(1).ComputeRasterMinMax()
    minmax = minmax + list(zr)
    with open('{}.inf'.format(src_ds.GetDescription()), 'w') as inf:
        echo_msg('generating inf file for {}'.format(src_ds.GetDescription()))
        inf.write('{}\n'.format(' '.join([str(x) for x in minmax])))
    return(minmax)
                    
def gdal_inf_entry(entry, warp = None):
    ''' scan a gdal entry and find it's region

    returns the region [xmin, xmax, ymin, ymax] of the gdal entry'''
    
    ds = gdal.Open(entry[0])
    minmax = gdal_inf(ds, warp)
    ds = None
    return(minmax)

def gdal_yield_entry(entry, region = None, verbose = False, epsg = None, z_region = None):
    '''yield the xyz data from the datalist entry.

    yields [x, y, z, <w, ...>]'''
    
    ds = gdal.Open(entry[0])
    if region is not None:
        srcwin = gdal_srcwin(ds, region)
    else: srcwin = None
    for xyz in gdal_parse(ds, dump_nodata = False, srcwin = srcwin, warp = epsg, verbose = verbose, z_region = z_region):
        yield(xyz + [entry[2]] if entry[2] is not None else xyz)
    ds = None
    
def gdal_dump_entry(entry, dst_port = sys.stdout, region = None, verbose = False, epsg = None, z_region = None):
    '''dump the xyz data from the gdal entry to dst_port'''
    
    for xyz in gdal_yield_entry(entry, region, verbose, epsg, z_region):
        xyz_line(xyz, dst_port, True)

## ==============================================
## fetches processing (datalists fmt:400 - 499)
## ==============================================
def fetch_yield_entry(entry = ['nos:datatype=xyz'], region = None, verbose = False):
    '''yield the xyz data from the fetch module datalist entry

    yields [x, y, z, <w, ...>]'''
    
    fetch_mod = entry[0].split(':')[0]
    fetch_args = entry[0].split(':')[1:]
    
    fl = fetches.fetch_infos[fetch_mod][0](region_buffer(region, 5, pct = True), [], lambda: False)
    args_d = args2dict(fetch_args, {})
    fl._verbose = verbose

    for xyz in fl._yield_results_to_xyz(**args_d):
        yield(xyz + [entry[2]] if entry[2] is not None else xyz)

def fetch_dump_entry(entry = ['nos:datatype=nos'], dst_port = sys.stdout, region = None, verbose = False):
    '''dump the xyz data from the fetch module datalist entry to dst_port'''
    
    for xyz in fetch_yield_entry(entry, region, verbose):
        xyz_line(xyz, dst_port, True)
        
def fetch_module_yield_entry(entry, region = None, verbose = False, module = 'dc'):
    '''yield the xyz data from the fetch module datalist entry

    yields [x, y, z, <w, ...>]'''
    
    fl = fetches.fetch_infos[module][0](region_buffer(region, 5, pct = True), [], lambda: False)
    fl._verbose = verbose
    fetch_entry = [entry[0], entry[0].split('/')[-1], module]
    
    for xyz in fl._yield_xyz(fetch_entry):
        yield(xyz + [entry[2]] if entry[2] is not None else xyz)

def fetch_dc_dump_entry(entry, dst_port = sys.stdout, region = None, verbose = False, module = 'dc'):
    '''dump the xyz data from the fetch module datalist entry to dst_port'''
    
    for xyz in fetch_module_yield_entry(entry, region, verbose, module):
        xyz_line(xyz, dst_port, True)        
        
## ==============================================
## xyz processing (datalists fmt:168)
## ==============================================
_xyz_config = {
    'delim': None,
    'xpos': 0,
    'ypos': 1,
    'zpos': 2,
    'skip': 0,
    'name': '<xyz-data-stream>',
    'upper_limit': None,
    'lower_limit': None}

_known_delims = [',', ' ', '\t', '/', ':']

def xyz_line_delim(xyz):
    for delim in _known_delims:
        this_xyz = xyz.split(delim)
        if len(this_xyz) > 1: return(delim)
    return(None)

def xyz_parse_line(xyz, xyz_c = _xyz_config):
    '''parse an xyz line-string, using _xyz_config

    returns [x, y, z]'''
    
    this_line = xyz.strip()
    if xyz_c['delim'] is None:
        xyz_c['delim'] = xyz_line_delim(this_line)
    this_xyz = this_line.split(xyz_c['delim'])
    try:
        o_xyz = [float(this_xyz[xyz_c['xpos']]), float(this_xyz[xyz_c['ypos']]), float(this_xyz[xyz_c['zpos']])]
    except IndexError as e:
        echo_error_msg(e)
        return(None)
    except Exception as e:
        echo_error_msg(e)
        return(None)
    return(o_xyz)
    
def xyz_parse(src_xyz, xyz_c = _xyz_config, region = None, verbose = False):
    '''xyz file parsing generator
    `src_xyz` is a file object or list of xyz data.

    yields each xyz line as a list [x, y, z, ...]'''
    
    ln = 0
    skip = int(xyz_c['skip'])
    xpos = xyz_c['xpos']
    ypos = xyz_c['ypos']
    zpos = xyz_c['zpos']
    pass_d = True
    #if verbose: echo_msg('parsing xyz data from {}...'.format(xyz_c['name']))
    for xyz in src_xyz:
        pass_d = True
        if ln >= skip:
            this_xyz = xyz_parse_line(xyz, xyz_c)
            if this_xyz is not None:
                if region is not None:
                    if not xyz_in_region_p(this_xyz, region): pass_d = False
                if xyz_c['upper_limit'] is not None or xyz_c['lower_limit'] is not None:
                    if not z_pass(this_xyz[2], upper_limit = xyz_c['upper_limit'], lower_limit = xyz_c['lower_limit']): pass_d = False
            else: pass_d = False
            
            if pass_d:
                ln += 1
                yield(this_xyz)
        else: skip -= 1
    if verbose: echo_msg('parsed {} data records from {}'.format(ln, xyz_c['name']))

def xyz2py(src_xyz):
    '''return src_xyz as a python list'''
    
    xyzpy = []
    return([xyzpy.append(xyz) for xyz in xyz_parse(src_xyz)])

def xyz_block(src_xyz, region, inc, dst_xyz = sys.stdout, weights = False, verbose = False):
    '''block the src_xyz data to the mean block value

    yields the xyz value for each block with data'''
    
    xcount, ycount, dst_gt = gdal_region2gt(region, inc)
    #xcount += 1
    #ycount += 1
    sumArray = np.zeros((ycount, xcount))
    gdt = gdal.GDT_Float32
    ptArray = np.zeros((ycount, xcount))
    if weights: wtArray = np.zeros((ycount, xcount))
    if verbose: echo_msg('blocking data to {}/{} grid'.format(ycount, xcount))
    for this_xyz in src_xyz:
        x = this_xyz[0]
        y = this_xyz[1]
        z = this_xyz[2]
        if weights:
            w = this_xyz[3]
            z = z * w
        if x > region[0] and x < region[1]:
            if y > region[2] and y < region[3]:
                xpos, ypos = _geo2pixel(x, y, dst_gt)
                try:
                    sumArray[ypos, xpos] += z
                    ptArray[ypos, xpos] += 1
                    if weights: wtArray[ypos, xpos] += w
                except: pass
    ptArray[ptArray == 0] = np.nan
    if weights:
        wtArray[wtArray == 0] = 1
        outarray = (sumArray / wtArray) / ptArray
    else: outarray = sumArray / ptArray

    sumArray = ptArray = None
    if weights: wtArray = None

    outarray[np.isnan(outarray)] = -9999
    
    for y in range(0, ycount):
        for x in range(0, xcount):
            geo_x, geo_y = _pixel2geo(x, y, dst_gt)
            z = outarray[y,x]
            if z != -9999:
                yield([geo_x, geo_y, z])
    
def xyz_line(line, dst_port = sys.stdout, encode = False):
    '''write "xyz" `line` to `dst_port`
    `line` should be a list of xyz values [x, y, z, ...].'''
    delim = _xyz_config['delim'] if _xyz_config['delim'] is not None else ' '
    
    l = '{}\n'.format(delim.join([str(x) for x in line]))
    if encode: l = l.encode('utf-8')
    dst_port.write(l)

def xyz_in_region_p(src_xy, src_region):
    '''return True if point [x, y] is inside region [w, e, s, n], else False.'''
    
    if src_xy[0] < src_region[0]: return(False)
    elif src_xy[0] > src_region[1]: return(False)
    elif src_xy[1] < src_region[2]: return(False)
    elif src_xy[1] > src_region[3]: return(False)
    else: return(True)
    
def xyz_inf(src_xyz):
    '''scan an xyz file and find it's min/max values and
    write an associated inf file for the src_xyz file.

    returns region [xmin, xmax, ymin, ymax] of the src_xyz file.'''
    
    minmax = []
    for i,l in enumerate(xyz_parse(src_xyz)):
        if i == 0:
            minmax = [l[0], l[0], l[1], l[1], l[2], l[2]]
        else:
            try:
                if l[0] < minmax[0]: minmax[0] = l[0]
                elif l[0] > minmax[1]: minmax[1] = l[0]
                if l[1] < minmax[2]: minmax[2] = l[1]
                elif l[1] > minmax[3]: minmax[3] = l[1]
                if l[2] < minmax[4]: minmax[4] = l[2]
                elif l[2] > minmax[5]: minmax[5] = l[2]
            except: pass
    if len(minmax) == 6:
        with open('{}.inf'.format(src_xyz.name), 'w') as inf:
            #echo_msg('generating inf file for {}'.format(src_xyz.name))
            inf.write('{}\n'.format(' '.join([str(x) for x in minmax])))
        return(minmax)
    else: return(0,0,0,0,0,0)

def xyz_inf_entry(entry):
    '''find the region of the xyz datalist entry
    
    returns the region [xmin, xmax, ymin, ymax, zmin, zmax] of the xyz entry'''
    
    with open(entry[0]) as infile:
        try:
            minmax = mb_inf(infile)
        except: minmax = xyz_inf(infile)
    return(minmax)        

def xyz_yield_entry(entry, region = None, verbose = False, z_region = None):
    '''yield the xyz data from the xyz datalist entry

    yields [x, y, z, <w, ...>]'''

    xyzc = copy.deepcopy(_xyz_config)
    xyzc['name'] = entry[0]
    if z_region is not None and len(z_region) >= 2:
        xyzc['lower_limit'] = z_region[0]
        xyzc['upper_limit'] = z_region[1]
    
    with open(entry[0]) as infile:
        for line in xyz_parse(infile, xyz_c = xyzc, region = region, verbose = verbose):
            yield(line + [entry[2]] if entry[2] is not None else line)

def gmt_yield_entry(entry, region = None, verbose = False, z_region = None):
    '''yield the xyz data from the xyz datalist entry

    yields [x, y, z, <w, ...>]'''
    ln = 0
    delim = None
    if z_region is not None:
        z_region = ['-' if x is None else str(x) for x in z_region]
    out, status = run_cmd('gmt gmtset IO_COL_SEPARATOR = SPACE', verbose = False)
    for line in yield_cmd('gmt gmtselect -V {} {} {}\
    '.format(entry[0], '' if region is None else region_format(region, 'gmt'), '' if z_region is None else '-Z{}'.format('/'.join(z_region))),
                          data_fun = None, verbose = False):
        ln += 1
        if delim is None: delim = xyz_line_delim(line)
        xyz = [float(x) for x in line.split(delim)]
        yield(xyz + [entry[2]] if entry[2] is not None else xyz)
    if verbose: echo_msg('read {} data points from {}'.format(ln, entry[0]))
        
def xyz_dump_entry(entry, dst_port = sys.stdout, region = None, verbose = False, z_region = None):
    '''dump the xyz data from the xyz datalist entry to dst_port'''
    
    for xyz in xyz_yield_entry(entry, region, verbose, z_region):
        xyz_line(xyz, dst_port, True, None)

## ==============================================
## datalists and entries - datalists.py
##
## datalist processing (datalists fmt:-1)
## entry processing fmt:*
## ==============================================
_known_dl_delims = [' ']
_known_datalist_fmts = {-1: ['datalist', 'mb-1'], 168: ['xyz', 'csv', 'dat', 'ascii'], 200: ['tif', 'img', 'grd', 'nc', 'vrt', 'bag'], 400: ['nos', 'dc', 'gmrt', 'srtm', 'charts', 'mb']}
_known_datalist_fmts_short_desc = lambda: '\n  '.join(['{}\t{}'.format(key, _known_datalist_fmts[key]) for key in _known_datalist_fmts])
_dl_inf_h = {
    -1: lambda e: datalist_inf_entry(e),
    168: lambda e: xyz_inf_entry(e),
    200: lambda e: gdal_inf_entry(e)
}
_dl_pass_h = lambda e: path_exists_or_url(e[0])

def datalist_inf(dl, inf_file = True, overwrite = False):
    '''return the region of the datalist and generate
    an associated `.inf` file if `inf_file` is True.'''
    
    out_regions = []
    minmax = None
    dh_p = lambda e: region_valid_p(inf_entry(e, True)) if e[1] == -1 else region_valid_p(inf_entry(e))
    for entry in datalist(dl, pass_h = dh_p):
        #entry_inf = inf_entry(entry, True) if entry[1] == -1 else inf_entry(entry)
        if entry[1] == 200:
            entry_inf = inf_entry(entry, True)
        else: entry_inf = inf_entry(entry)
        if entry_inf is not None:
            out_regions.append(inf_entry(entry)[:6])
    
    out_regions = [x for x in out_regions if x is not None]
    if len(out_regions) == 0:
        minmax = None
    elif len(out_regions) == 1:
        minmax = out_regions[0]
    else:
        out_region = out_regions[0]
        for x in out_regions[1:]:
            out_region = regions_merge(out_region, x)
        minmax = out_region
    if minmax is not None and inf_file:
        echo_msg('generating inf for datalist {}'.format(dl))
        with open('{}.inf'.format(dl), 'w') as inf:
            inf.write('{}\n'.format(region_format(minmax, 'inf')))
    return(minmax)

def datalist_inf_entry(e):
    '''write an inf file for datalist entry e
    
    return the region [xmin, xmax, ymin, ymax]'''
    
    return(datalist_inf(e[0]))

def datalist_append_entry(entry, datalist):
    '''append entry to datalist file `datalist`'''
    
    with open(datalist, 'a') as outfile:
        outfile.write('{}\n'.format(' '.join([str(x) for x in entry])))

#def datalist_polygonize_datalist(dl, layer = None):
    
def datalist_archive_yield_entry(entry, dirname = 'archive', region = None, inc = 1, weight = None, verbose = None, z_region = None):
    '''archive a datalist entry.
    a datalist entry is [path, format, weight, ...]'''
    
    if region is None:
        a_name = entry[-1]
    else: a_name = '{}_{}_{}'.format(entry[-1], region_format(region, 'fn'), this_year())
    i_dir = os.path.dirname(entry[0])
    i_xyz = os.path.basename(entry[0]).split('.')[0]
    i_xyz = ''.join(x for x in i_xyz if x.isalnum())
    a_dir = os.path.join(dirname, a_name, 'data', entry[-1])
    a_xyz_dir = os.path.join(a_dir, 'xyz')
    a_xyz = os.path.join(a_xyz_dir, i_xyz + '.xyz')
    a_dl = os.path.join(a_xyz_dir, '{}.datalist'.format(entry[-1]))
    
    if not os.path.exists(a_dir): os.makedirs(a_dir)
    if not os.path.exists(a_xyz_dir): os.makedirs(a_xyz_dir)

    with open(a_xyz, 'w') as fob:
        for xyz in datalist_yield_entry(entry, region = region, verbose = verbose, z_region = z_region):
            xyz_line(xyz, fob)
            yield(xyz)
            
    mb_inf(a_xyz)
    datalist_append_entry([i_xyz + '.xyz', 168, entry[2] if entry[2] is not None else 1], a_dl)
    
def datalist_archive(wg, arch_dir = 'archive', region = None, verbose = False, z_region = None):
    '''archive the data from wg_config datalist to `arch_dir`

    returns the datalist of the archive'''
    
    if region is not None:
        dl_p = lambda e: regions_intersect_ogr_p(region, inf_entry(e))
    else: dl_p = _dl_pass_h

    for this_entry in datalist(wg['datalist'], wt = 1 if wg['weights'] else None, pass_h = dl_p, verbose = verbose):
        for xyz in datalist_archive_yield_entry(this_entry, dirname = arch_dir, region = region, verbose = verbose, z_region = z_region):
            pass
    a_dl = os.path.join(arch_dir, '{}.datalist'.format(wg['name']))
            
    for dir_, _, files in os.walk(arch_dir):
        for f in files:
            if '.datalist' in f:
                rel_dir = os.path.relpath(dir_, arch_dir)
                rel_file = os.path.join(rel_dir, f)
                datalist_append_entry([rel_file, -1, 1], a_dl)
    return(a_dl, 0)

def datalist_list(wg):
    '''list the datalist entries in the given region'''
    if wg['region'] is not None:
        dl_p = lambda e: regions_intersect_ogr_p(wg['region'], inf_entry(e))
    else: dl_p = _dl_pass_h
    for this_entry in datalist(wg['datalist'], wt = 1, pass_h = dl_p):
        print(' '.join([','.join(x) if i == 3 else str(x) for i,x in enumerate(this_entry[:-1])]))
    
def datalist_echo(entry):
    '''echo datalist entry to stderr'''
    
    sys.stderr.write('{}\n'.format([str(x) for x in entry]))
    datalist(entry[0])

def datafile_echo(entry):
    '''echo datafile entry to stderr'''
    
    sys.stderr.write('{}\n'.format([str(x) for x in entry]))

def datalist_major(dls, major = '.mjr.datalist', region = None):
    '''set the major datalist
    `dls` is a list of datalist entries, minimally: ['datafile.xyz']

    returns the major datalist filename'''
    
    with open(major, 'w') as md:        
        for dl in dls:
            entries = datalist2py(dl, region)
            for entry in entries:
                md.write('{}\n'.format(' '.join([str(e) for e in entry])))
    if os.stat(major).st_size == 0:
        remove_glob(major)
        echo_error_msg('bad datalist/entry, {}'.format(dls))
        return(None)    
    return(major)

def entry2py(dle):
    '''convert a datalist entry to python

    return the entry as a list [fn, fmt, wt, ...]'''
    this_entry = dle.rstrip().split()
    try:
        entry = [x if n == 0 else int(x) if n < 2 else float(x) if n < 3 else x for n, x in enumerate(this_entry)]
    except Exception as e:
        echo_error_msg('could not parse entry {}'.format(dle))
        return(None)
    if len(entry) < 2:
        for key in _known_datalist_fmts.keys():
            se = entry[0].split('.')
            if len(se) == 1:
                see = entry[0].split(':')[0]
            else: see = se[-1]
            if see in _known_datalist_fmts[key]:
                entry.append(int(key))
    if len(entry) < 3: entry.append(1)
    return(entry)

def datalist2py(dl, region = None):
    '''convert a datalist to python data
    
    returns a list of datalist entries.'''
    these_entries = []
    this_dir = os.path.dirname(dl)
    this_entry = entry2py(dl)
    if this_entry[1] == -1:
        with open(this_entry[0], 'r') as op:
            for this_line in op:
                if this_line[0] != '#' and this_line[0] != '\n' and this_line[0].rstrip() != '':
                    #these_entries.append([os.path.join(this_dir, x) if n == 0 else x for n,x in enumerate(entry2py(this_line.rstrip()))])
                    these_entries.append(entry2py(this_line.rstrip()))
    elif this_entry[1] == 400:
        fetch_mod = this_entry[0].split(':')[0]
        fetch_args = this_entry[0].split(':')[1:]
        if fetch_mod in fetches.fetch_infos.keys():
            fl = fetches.fetch_infos[fetch_mod][0](region_buffer(region, 5, pct = True), [], lambda: False)
            args_d = args2dict(fetch_args, {})
            fl._verbose = True

            results = fl.run(**args_d)
            if len(results) > 0:
                with open('{}.datalist'.format(fetch_mod), 'w') as fdl:
                    for r in results:
                        e = [r[0], fl._datalists_code, 1]
                        fdl.write('{} {} {}\n'.format(e[0], e[1], e[2]))
                these_entries.append(['{}.datalist'.format(fetch_mod), -1, 1])
                
    else: these_entries.append(this_entry)
    return(these_entries)

def datalist_yield_entry(this_entry, region, verbose = False, z_region = None):
    if this_entry[1] == 168:
        for xyz in xyz_yield_entry(this_entry, region = region, verbose = verbose, z_region = z_region):
            yield(xyz)
    elif this_entry[1] == 200:
        for xyz in gdal_yield_entry(this_entry, region = region, verbose = verbose, z_region = z_region):
            yield(xyz)
    elif this_entry[1] == 400:
        for xyz in fetch_yield_entry(this_entry, region = region, verbose = verbose):
            yield(xyz)
    elif this_entry[1] == 401:
        for xyz in fetch_module_yield_entry(this_entry, region, verbose, 'nos'):
            yield(xyz)
    elif this_entry[1] == 402:
        for xyz in fetch_module_yield_entry(this_entry, region, verbose, 'dc'):
            yield(xyz)
    elif this_entry[1] == 403:
        for xyz in fetch_module_yield_entry(this_entry, region, verbose, 'charts'):
            yield(xyz)
    elif this_entry[1] == 404:
        for xyz in fetch_module_yield_entry(this_entry, region, verbose, 'srtm'):
            yield(xyz)
    elif this_entry[1] == 406:
        for xyz in fetch_module_yield_entry(this_entry, region, verbose, 'mb'):
            yield(xyz)
    elif this_entry[1] == 408:
        for xyz in fetch_module_yield_entry(this_entry, region, verbose, 'gmrt'):
            yield(xyz)
            

def datalist_yield_xyz(dl, fmt = -1, wt = None,
                       pass_h = lambda e: True,
                       dl_proc_h = False, region = None, archive = False,
                       mask = False, verbose = False, z_region = None):
    '''parse out the xyz data from the datalist
    for xyz in datalist_yield_xyz(dl): xyz_line(xyz)

    yields xyz line data [x, y, z, ...]'''

    for this_entry in datalist(dl, fmt = fmt, wt = wt, pass_h = pass_h, dl_proc_h = dl_proc_h, verbose = verbose):
        dly = datalist_yield_entry(this_entry, region, verbose = verbose, z_region = z_region)
        if archive: dly = datalist_archive_yield_entry(this_entry, dirname = 'archive', region = region, weight = wt, verbose = verbose, z_region = z_region)
        for xyz in dly:
            yield(xyz)

def datalist_dump_xyz(dl, fmt = -1, wt = None,
                      pass_h = lambda e: True,
                      dl_proc_h = False,
                      region = None, archive = False, mask = False,
                      verbose = False, dst_port = sys.stdout, z_region = None):
    '''parse out the xyz data from the datalist
    for xyz in datalist_yield_xyz(dl): xyz_line(xyz)

    yields xyz line data [x, y, z, ...]'''

    for xyz in datalist_yield_xyz(dl, fmt, wt, pass_h, dl_proc_h, region, archive, mask, verbose, z_region):
        xyz_line(xyz, dst_port, verbose)
            
def datalist(dl, fmt = -1, wt = None,
             pass_h = lambda e: True,
             dl_proc_h = False, verbose = False):
    '''recurse a datalist/entry
    for entry in datalist(dl): do_something_with entry

    yields entry [path, fmt, wt, ...]'''

    this_dir = os.path.dirname(dl)
    these_entries = datalist2py(dl)
    if len(these_entries) == 0: these_entries = [entry2py(dl)]
    for this_entry in these_entries:
        if this_entry is not None:
            this_entry[0] = os.path.join(this_dir, this_entry[0])
            if wt is not None:
                this_entry[2] = wt * this_entry[2]
            else: this_entry[2] = wt
            this_entry_md = ' '.join(this_entry[3:]).split(',')
            this_entry = this_entry[:3] + [this_entry_md] + [os.path.basename(dl).split('.')[0]]
            if path_exists_or_url(this_entry[0]) and pass_h(this_entry):
                if verbose and this_entry[1] == -1: echo_msg('parsing datalist ({}) {}'.format(this_entry[2], this_entry[0]))
                if this_entry[1] == -1:
                    if dl_proc_h: yield(this_entry)
                    #dl_proc_h(this_entry)
                    for entry in datalist(this_entry[0], fmt, this_entry[2], pass_h, dl_proc_h, verbose):
                        yield(entry)
                else: yield(this_entry)
            
## ==============================================
## DEM module: generate a Digital Elevation Model using a variety of methods
## dem modules include: 'mbgrid', 'surface', 'num', 'mean'
##
## Requires MBSystem, GMT, GDAL and VDatum for full functionality
## ==============================================
_waffles_grid_info = {
    'datalist': None,
    'datalists': [],
    'region': None,
    'inc': None,
    'name': 'waffles_dem',
    'name_prefix': None,
    'node': 'pixel',
    'fmt': 'GTiff',
    'extend': 0,
    'extend_proc': 20,
    'weights': None,
    'upper_limit': None,
    'fltr': None,
    'sample': None,
    'clip': None,
    'chunk': None,
    'epsg': 4326,
    'mod': 'help',
    'mod_args': (),
    'verbose': False,
    'archive': False,
    'spat': False,
    'mask': False,
    'unc': False,
    'gc': config_check()
}

## ==============================================
## the default waffles config dictionary.
## lambda returns dictionary with default waffles
## ==============================================
waffles_config = lambda: copy.deepcopy(_waffles_grid_info)
    
def waffles_dict2wg(wg = _waffles_grid_info):
    '''copy the `wg` dict and add any missing keys.
    also validate the key values and return the valid waffles_config
    
    returns a complete and validated waffles_config dict.'''

    wg = copy.deepcopy(wg)
    keys = wg.keys()
    ## ==============================================
    ## check the waffles config dict and set
    ## missing values and their defaults.
    ## ==============================================
    if 'datalist' not in keys: wg['datalist'] = None
    if 'datalists' not in keys:
        if wg['datalist'] is not None:
            wg['datalists'] = [x[0] for x in datalist2py(wg['datalist'])]
        else: wg['datalists'] = None
    if 'region' not in keys: wg['region'] = None
    if 'inc' not in keys: wg['inc'] = None
    else: wg['inc'] = gmt_inc2inc(str(wg['inc']))
    if 'name' not in keys: wg['name'] = 'waffles_dem'
    if 'name_prefix' not in keys: wg['name_prefix'] = None
    if 'node' not in keys: wg['node'] = 'pixel'
    if 'fmt' not in keys: wg['fmt'] = 'GTiff'
    if 'extend' not in keys: wg['extend'] = 0
    else: wg['extend'] = int_or(wg['extend'], 0)
    if 'extend_proc' not in keys: wg['extend_proc'] = 10
    else: wg['extend_proc'] = int_or(wg['extend_proc'], 10)
    if 'weights' not in keys: wg['weights'] = None
    if 'upper_limit' not in keys: wg['upper_limit'] = None
    if 'lower_limit' not in keys: wg['lower_limit'] = None
    if 'fltr' not in keys: wg['fltr'] = None
    if 'sample' not in keys: wg['sample'] = None
    else: wg['sample'] = gmt_inc2inc(str(wg['sample']))
    if 'clip' not in keys: wg['clip'] = None
    if 'chunk' not in keys: wg['chunk'] = None
    else: wg['chunk'] = int_or(wg['chunk'], None)
    if 'epsg' not in keys: wg['epsg'] = 4326
    else: wg['epsg'] = int_or(wg['epsg'], 4326)
    if 'mod' not in keys: wg['mod'] = 'help'
    if 'mod_args' not in keys: wg['mod_args'] = ()
    if 'verbose' not in keys: wg['verbose'] = False
    else: wg['verbose'] = False if not wg['verbose'] or str(wg['verbose']).lower() == 'false' or wg['verbose'] is None else True
    if 'archive' not in keys: wg['archive'] = False
    else: wg['archive'] = False if not wg['archive'] or str(wg['archive']).lower() == 'false' or wg['archive'] is None else True
    if 'spat' not in keys: wg['spat'] = False
    else: wg['spat'] = False if not wg['spat'] or str(wg['spat']).lower() == 'false' or wg['spat'] is None else True
    if 'mask' not in keys: wg['mask'] = False
    else: wg['mask'] = False if not wg['mask'] or str(wg['mask']).lower() == 'false' or wg['mask'] is None else True
    if 'unc' not in keys: wg['unc'] = False
    else: wg['unc'] = False if not wg['unc'] or str(wg['unc']).lower() == 'false' or wg['unc'] is None else True
    wg['gc'] = config_check()
    
    ## ==============================================
    ## set the major datalist to the mentioned
    ## datalists/datasets
    ## note: the vdatum module doesn't need a datalist
    ## ==============================================
    if wg['datalist'] is None and len(wg['datalists']) > 0:
        wg['datalist'] = datalist_major(wg['datalists'], region = wg['region'])
    if wg['mod'].lower() != 'vdatum':
        if wg['datalist'] is None:
            echo_error_msg('invalid datalist/s entry')
            return(None)
        
        ## ==============================================
        ## set the region to that of the datalist if
        ## the region was not specified.
        ## ==============================================
        if wg['region'] is None or not region_valid_p(wg['region']):
            wg['region'] = datalist_inf(wg['datalist'])

    if wg['region'] is None:
        echo_error_msg('invalid region and/or datalist/s entry')
        return(None)
    if wg['inc'] is None: wg['inc'] = (wg['region'][1] - wg['region'][0]) / 500
    
    ## ==============================================
    ## if `name_prefix` is set; append region/inc/year
    ## to `prefix_name` and set `name` to that.
    ## ==============================================
    if wg['name_prefix'] is not None:
        wg['name'] = waffles_append_fn(wg['name_prefix'], wg['region'], wg['sample'] if wg['sample'] is not None else wg['inc'])        
    return(wg)

_waffles_modules = {
    'surface': [lambda args: waffles_gmt_surface(**args), '''SPLINE DEM via GMT surface
    \t\t\t  < surface:tension=.35:relaxation=1.2:lower_limit=d:upper_limit=d >
    \t\t\t  :tension=[0-1] - Spline tension.'''],
    'triangulate': [lambda args: waffles_gmt_triangulate(**args), '''TRIANGULATION DEM via GMT triangulate'''],
    'nearest': [lambda args: waffles_nearneighbor(**args), '''NEAREST NEIGHBOR DEM via GMT or gdal_grid
    \t\t\t  < nearest:radius=6s:use_gdal=False >
    \t\t\t  :radius=[value] - Nearest Neighbor search radius
    \t\t\t  :use_gdal=[True/False] - use gdal grid nearest algorithm'''],
    'num': [lambda args: waffles_num(**args), '''Uninterpolated DEM populated by <mode>.
    \t\t\t  < num:mode=n >
    \t\t\t  :mode=[key] - specify mode of grid population: k (mask), m (mean) or n (num)'''],
    'vdatum': [lambda args: waffles_vdatum(**args), '''VDATUM transformation grid
    \t\t\t  < vdatum:ivert=navd88:overt=mhw:region=3:jar=None >
    \t\t\t  :ivert=[vdatum] - Input VDatum vertical datum.
    \t\t\t  :overt=[vdatum] - Output VDatum vertical datum.
    \t\t\t  :region=[0-10] - VDatum region (3 is CONUS).
    \t\t\t  :jar=[/path/to/vdatum.jar] - VDatum jar path - (auto-locates by default)'''],
    'mbgrid': [lambda args: waffles_mbgrid(**args), '''Weighted SPLINE DEM via mbgrid
    \t\t\t  < mbgrid:tension=35:dist=10/3:use_datalists=False >
    \t\t\t  :tension=[0-100] - Spline tension.
    \t\t\t  :dist=[value] - MBgrid -C switch (distance to fill nodata with spline)
    \t\t\t  :use_datalists=[True/False] - use waffles built-in datalists'''],
    'invdst': [lambda args: waffles_invdst(**args), '''INVERSE DISTANCE DEM via gdal_grid
    \t\t\t  < invdst:power=2.0:smoothing=0.0:radus1=0.1:radius2:0.1 >'''],
    'average': [lambda args: waffles_moving_average(**args), '''Moving AVERAGE DEM via gdal_grid
    \t\t\t  < average:radius1=0.01:radius2=0.01 >'''],
    'help': [lambda args: waffles_help(**args), '''display module info'''],
    'datalists': [lambda args: waffles_datalists(**args), '''recurse the DATALIST
    \t\t\t  < datalists:dump=False:echo=False:infos=False:recurse=True >
    \t\t\t  :dump=[True/False] - dump the data from the datalist(s)
    \t\t\t  :echo=[True/False] - echo the data entries from the datalist(s)
    \t\t\t  :infos=[True/False] - generate inf files for the datalists datalist entries.
    \t\t\t  :recurse=[True/False] - recurse the datalist (default = True)'''],
}

## ==============================================
## module descriptors (used in cli help)
## ==============================================
_waffles_module_long_desc = lambda x: 'waffles modules:\n% waffles ... -M <mod>:key=val:key=val...\n\n  ' + '\n  '.join(['{:22}{}\n'.format(key, x[key][-1]) for key in x]) + '\n'
_waffles_module_short_desc = lambda x: ', '.join(['{}'.format(key) for key in x])

## ==============================================
## the "proc-region" region_buffer(wg['region'], (wg['inc'] * 20) + (wg['inc'] * wg['extend']))
## ==============================================
waffles_proc_region = lambda wg: region_buffer(wg['region'], (wg['inc'] * wg['extend_proc']) + (wg['inc'] * wg['extend']))
waffles_proc_str = lambda wg: region_format(waffles_proc_region(wg), 'gmt')
waffles_proc_bbox = lambda wg: region_format(waffles_proc_region(wg), 'bbox')
waffles_proc_ul_lr = lambda wg: region_format(waffles_proc_region(wg), 'ul_lr')

## ==============================================
## the "dist-region" region_buffer(wg['region'], (wg['inc'] * wg['extend']))
## ==============================================
waffles_dist_region = lambda wg: region_buffer(wg['region'], (wg['inc'] * wg['extend']))
waffles_dist_ul_lr = lambda wg: region_format(waffles_dist_region(wg), 'ul_lr')

## ==============================================
## the datalist dump function, to use in run_cmd()
## ==============================================
waffles_dl_func = lambda wg: lambda p: waffles_dump_datalist(wg, dst_port = p)

## ==============================================
## grid registration string for use in GTM programs
## ==============================================
waffles_gmt_reg_str = lambda wg: '-r' if wg['node'] == 'pixel' else ''

## ==============================================
## the 'long-name' used from prefix
## ==============================================
waffles_append_fn = lambda bn, region, inc: '{}{}_{}_{}'.format(bn, inc2str_inc(inc), region_format(region, 'fn'), this_year())

def waffles_help(wg = _waffles_grid_info):
    sys.stderr.write(_waffles_module_long_desc(_waffles_modules))
    return(0, 0)

def waffles_yield_datalist(wg = _waffles_grid_info):    
    wg['region'] = region_buffer(wg['region'], wg['inc'] * .5) if wg['node'] == 'grid' else wg['region']
    region = waffles_proc_region(wg)
    dlh = lambda e: regions_intersect_ogr_p(region, inf_entry(e))
    if wg['upper_limit'] is not None or wg['lower_limit'] is not None:
        dlh = lambda e: regions_intersect_ogr_p(region, inf_entry(e)) and z_region_pass(inf_entry(e), upper_limit = wg['upper_limit'], lower_limit = wg['lower_limit'])
        z_region = [wg['lower_limit'], wg['upper_limit']]
    else: z_region = None
    if wg['spat']:
        dly = waffles_spat_meta(wg, dlh)
    else:  dly = datalist_yield_xyz(wg['datalist'], pass_h = dlh, wt = 1 if wg['weights'] else None, region = region, archive = wg['archive'], verbose = wg['verbose'], z_region = z_region)
    if wg['mask']: dly = gdal_xyz_mask(dly, '{}_msk.tif'.format(wg['name']), region, wg['inc'], dst_format = wg['fmt'])
    for xyz in dly:
        yield(xyz)
    if wg['spat']: sm_ds = None
    if wg['archive']:
        a_dl = os.path.join('archive', '{}.datalist'.format(wg['name']))

        for dir_, _, files in os.walk('archive'):
            for f in files:
                if '.datalist' in f:
                    rel_dir = os.path.relpath(dir_, 'archive')
                    rel_file = os.path.join(rel_dir, f)
                    datalist_append_entry([rel_file, -1, 1], a_dl)

def waffles_dump_datalist(wg = _waffles_grid_info, dst_port = sys.stdout):
    '''dump the xyz data from datalist and generate a data mask while doing it.'''

    for xyz in waffles_yield_datalist(wg):
        xyz_line(xyz, dst_port, True)
        
def waffles_datalists(wg = _waffles_grid_info, dump = False, echo = False, infos = False, recurse = True):
    '''dump the xyz data from datalist and generate a data mask while doing it.'''

    if echo: datalist_list(wg)
    if infos: print(datalist_inf(wg['datalist'], inf_file = True))
    if dump:
        recurse = True
        pass_func = lambda xyz: xyz_line(xyz, sys.stdout, True)
    else: pass_func = lambda xyz: None

    if recurse:
        for xyz in waffles_yield_datalist(wg): pass_func(xyz)
    return(0,0)

def waffles_polygonize_datalist(wg, entry, layer = None, dlh = lambda e: True,
                                v_fields = ['Name', 'Agency', 'Date', 'Type', 'Resolution', 'HDatum', 'VDatum', 'URL']):
    '''polygonize the datalist entry given options from waffles-config wg.
    the layer should be the major ogr layer to append polygon to.'''

    if layer is not None:
        defn = layer.GetLayerDefn()
    else: defn = None
    twg = waffles_dict2wg(wg)
    twg['datalist'] = entry[0]
    twg['name'] = os.path.basename(entry[0]).split('.')[0]
    twg['region'] = region_buffer(wg['region'], wg['inc'] * .5) if wg['node'] == 'grid' else wg['region']
    twg['inc'] = gmt_inc2inc('.3333333s') if twg['inc'] < gmt_inc2inc('.3333333s') else twg['inc']
    ng = '{}_msk.tif'.format(twg['name'])
    if len(entry[3]) == 8:
        o_v_fields = entry[3]
    else: o_v_fields = [twg['name'], 'Unknown', '0', 'xyz_elevation', 'Unknown', 'WGS84', 'NAVD88', 'URL']
    dly = datalist_yield_xyz(entry[0], pass_h = dlh, wt = 1 if twg['weights'] else None, region = waffles_proc_region(twg), archive = twg['archive'], verbose = twg['verbose'])
    for xyz in gdal_xyz_mask(dly, ng, waffles_dist_region(twg), twg['inc'], dst_format = twg['fmt']):
        yield(xyz)

    if gdal_infos(ng, True)['zr'][1] == 1:
        tmp_ds = ogr.GetDriverByName('ESRI Shapefile').CreateDataSource('{}_poly.shp'.format(twg['name']))
        tmp_layer = tmp_ds.CreateLayer('{}_poly'.format(twg['name']), None, ogr.wkbMultiPolygon)
        tmp_layer.CreateField(ogr.FieldDefn('DN', ogr.OFTInteger))
        gdal_polygonize(ng, tmp_layer, verbose = twg['verbose'])

        if len(tmp_layer) > 1:
            if defn is None: defn = tmp_layer.GetLayerDefn()
            out_feat = gdal_ogr_mask_union(tmp_layer, 'DN', defn)
            [out_feat.SetField(f, o_v_fields[i]) for i, f in enumerate(v_fields)]
            layer.CreateFeature(out_feat)

        tmp_ds = tmp_layer = out_feat = None
        if layer is not None:
            remove_glob('{}_poly.*'.format(twg['name']))
    remove_glob(ng)

def waffles_spat_meta(wg, dlh = lambda e: True):

    dst_vector = '{}_sm.shp'.format(wg['name'])
    dst_layer = '{}_sm'.format(wg['name'])
    v_fields = ['Name', 'Agency', 'Date', 'Type', 'Resolution', 'HDatum', 'VDatum', 'URL']
    t_fields = [ogr.OFTString, ogr.OFTString, ogr.OFTString, ogr.OFTString, ogr.OFTString, ogr.OFTString, ogr.OFTString, ogr.OFTString]
    remove_glob('{}.*'.format(dst_layer))
    gdal_prj_file('{}.prj'.format(dst_layer), wg['epsg'])
    
    ds = ogr.GetDriverByName('ESRI Shapefile').CreateDataSource(dst_vector)
    if ds is not None: 
        layer = ds.CreateLayer('{}'.format(dst_layer), None, ogr.wkbMultiPolygon)
        [layer.CreateField(ogr.FieldDefn('{}'.format(f), t_fields[i])) for i, f in enumerate(v_fields)]
        [layer.SetFeature(feature) for feature in layer]
    else: layer = None
    defn = layer.GetLayerDefn()
    
    dlh_1 = lambda e: False if e[1] != -1 else regions_intersect_ogr_p(waffles_dist_region(wg), inf_entry(e))
    for this_entry in datalist(wg['datalist'], pass_h = dlh_1, dl_proc_h = True, verbose = wg['verbose']):
        if this_entry[1] == -1:
            for xyz in waffles_polygonize_datalist(wg, this_entry, layer = layer, dlh = dlh):
                yield(xyz)
    ds = None
        
def waffles_cudem(wg = _waffles_grid_info, upper_limit = 'd'):
    '''generate bathy/topo DEM'''
    
    if wg['gc']['GMT'] is None:
        echo_error_msg('GMT must be installed to use the BATHY-SURFACE module')
        return(None, -1)

    ## bathy-surface
    #costline = wg['clip']
    wg['upper_limit'] = 0 if upper_limit == 'd' else upper_limit

    ## add bs to datalist
    ## final dem with bs and minus low weight < 1
    dem_surf_cmd = ('gmt blockmean {} -I{:.10f}{} -V {} | gmt surface -V {} -I{:.10f} -G{}.tif=gd+n-9999:GTiff -T.35 -Z1.2 -Lld -Lu{} {}\
    '.format(waffles_proc_str(wg), wg['inc'], ' -Wi' if wg['weights'] else '', waffles_gmt_reg_str(wg), waffles_proc_str(wg), \
             wg['inc'], wg['name'], upper_limit, waffles_gmt_reg_str(wg)))
    return(run_cmd(dem_surf_cmd, verbose = wg['verbose'], data_fun = waffles_dl_func(wg)))
        
def waffles_mbgrid(wg = _waffles_grid_info, dist = '10/3', tension = 35, use_datalists = False):
    '''Generate a DEM with MBSystem's mbgrid program.
    if `use_datalists` is True, will parse the datalist through
    waffles instead of mbsystem.'''
    
    if wg['gc']['MBGRID'] is None:
        echo_error_msg('MBSystem must be installed to use the MBGRID module')
        return(None, -1)
    if wg['gc']['GMT'] is None:
        echo_error_msg('GMT must be installed to use the MBGRID module')
        return(None, -1)

    if use_datalists:
        #datalist_archive(wg, arch_dir = '.mb_tmp_datalist', verbose = True)
        archive = wg['archive']
        wg['archive'] = True
        for xyz in waffles_yield_datalist(wg): pass
        wg['datalist'] = datalist_major(['archive/{}.datalist'.format(wg['name'])])
        wg['archive'] = archive

    wg['region'] = region_buffer(wg['region'], wg['inc'] * -.5) if wg['node'] == 'pixel' else wg['region']
    xsize, ysize, gt = gdal_region2gt(waffles_proc_region(wg), wg['inc'])
    
    if len(dist.split('/')) == 1: dist = dist + '/2'
    mbgrid_cmd = ('mbgrid -I{} {} -D{}/{} -O{} -A2 -G100 -F1 -N -C{} -S0 -X0.1 -T{} {} > mb_proc.txt \
    '.format(wg['datalist'], waffles_proc_str(wg), xsize, ysize, wg['name'], dist, tension, '-M' if wg['mask'] else ''))
    out, status = run_cmd(mbgrid_cmd, verbose = wg['verbose'])

    remove_glob('*.cmd')
    remove_glob('*.mb-1')
    gmt_grd2gdal('{}.grd'.format(wg['name']))
    remove_glob('{}.grd'.format(wg['name']))
    if use_datalists and not wg['archive']: shutil.rmtree('archive')
    if wg['mask']:
        remove_glob('*_sd.grd')
        num_grd = '{}_num.grd'.format(wg['name'])
        dst_msk = '{}_msk.tif=gd+n-9999:GTiff'.format(wg['name'])
        out, status = gmt_num_msk(num_grd, dst_msk, verbose = wg['verbose'])
        remove_glob(num_grd)
    if not use_datalists:
        if wg['spat'] or wg['archive']:
            for xyz in waffles_yield_datalist(wg): pass
    return(0, 0)

def waffles_gmt_surface(wg = _waffles_grid_info, tension = .35, relaxation = 1.2, lower_limit = 'd', upper_limit = 'd'):
    '''generate a DEM with GMT surface'''
    
    if wg['gc']['GMT'] is None:
        echo_error_msg('GMT must be installed to use the SURFACE module')
        return(None, -1)

    dem_surf_cmd = ('gmt blockmean {} -I{:.10f}{} -V {} | gmt surface -V {} -I{:.10f} -G{}.tif=gd+n-9999:GTiff -T{} -Z{} -Ll{} -Lu{} {}\
    '.format(waffles_proc_str(wg), wg['inc'], ' -Wi' if wg['weights'] else '', waffles_gmt_reg_str(wg), waffles_proc_str(wg), \
             wg['inc'], wg['name'], tension, relaxation, lower_limit, upper_limit, waffles_gmt_reg_str(wg)))
    return(run_cmd(dem_surf_cmd, verbose = wg['verbose'], data_fun = waffles_dl_func(wg)))

def waffles_gmt_triangulate(wg = _waffles_grid_info):
    '''generate a DEM with GMT surface'''
    
    if wg['gc']['GMT'] is None:
        echo_error_msg('GMT must be installed to use the TRIANGULATE module')
        return(None, -1)
    dem_tri_cmd = ('gmt blockmean {} -I{:.10f}{} -V {} | gmt triangulate {} -I{:.10f} -V -G{}.tif=gd+n-9999:GTiff {}\
    '.format(waffles_proc_str(wg), wg['inc'], ' -Wi' if wg['weights'] else '', waffles_gmt_reg_str(wg), waffles_proc_str(wg), \
             wg['inc'], wg['name'], waffles_gmt_reg_str(wg)))
    return(run_cmd(dem_tri_cmd, verbose = wg['verbose'], data_fun = waffles_dl_func(wg)))

def waffles_nearneighbor(wg = _waffles_grid_info, radius = None, use_gdal = False):
    '''genearte a DEM with GMT nearneighbor or gdal_grid nearest'''
    
    radius = wg['inc'] * 2 if radius is None else gmt_inc2inc(radius)
    if wg['gc']['GMT'] is not None and not use_gdal:
        dem_nn_cmd = ('gmt blockmean {} -I{:.10f}{} -V {} | gmt nearneighbor {} -I{:.10f} -S{} -V -G{}.tif=gd+n-9999:GTiff {}\
        '.format(waffles_proc_str(wg), wg['inc'], ' -Wi' if wg['weights'] else '', waffles_gmt_reg_str(wg), waffles_proc_str(wg), \
                 wg['inc'], radius, wg['name'], waffles_gmt_reg_str(wg)))
        return(run_cmd(dem_nn_cmd, verbose = wg['verbose'], data_fun = waffles_dl_func(wg)))
    else: return(waffles_gdal_grid(wg, 'nearest:radius1={}:radius2={}:nodata=-9999'.format(radius, radius)))
    
def waffles_num(wg = _waffles_grid_info, mode = 'n'):
    '''Generate an uninterpolated num grid.
    mode of `k` generates a mask grid
    mode of `m` generates a mean grid
    mode of `n` generates a num grid'''
    
    wg['region'] = region_buffer(wg['region'], wg['inc'] * .5) if wg['node'] == 'grid' else wg['region']
    region = waffles_proc_region(wg)
    dlh = lambda e: regions_intersect_ogr_p(region, inf_entry(e))
    dly = waffles_yield_datalist(wg)
    if wg['weights']: dly = xyz_block(dly, region, wg['inc'], weights = True if wg['weights'] else False)
    return(gdal_xyz2gdal(dly, '{}.tif'.format(wg['name']), region, wg['inc'], dst_format = wg['fmt'], mode = mode, verbose = wg['verbose']))

def waffles_gdal_grid(wg = _waffles_grid_info, alg_str = 'linear:radius=1'):
    '''run gdal grid using alg_str
    parse the data through xyz_block to get weighted mean before
    building the GDAL dataset to pass into gdal_grid'''
    
    wg['region'] = region_buffer(wg['region'], wg['inc'] * .5) if wg['node'] == 'grid' else wg['region']
    region = waffles_proc_region(wg)
    dlh = lambda e: regions_intersect_ogr_p(region, inf_entry(e))
    wt = 1 if wg['weights'] is not None else None
    dly = xyz_block(waffles_yield_datalist(wg), region, wg['inc'], weights = False if wg['weights'] is None else True)
    ds = xyz2gdal_ds(dly, '{}'.format(wg['name']))
    xcount, ycount, dst_gt = gdal_region2gt(region, wg['inc'])
    gd_opts = gdal.GridOptions(outputType = gdal.GDT_Float32, noData = -9999, format = 'GTiff', \
                               width = xcount, height = ycount, algorithm = alg_str, callback = _gdal_progress if wg['verbose'] else None, \
                               outputBounds = [region[0], region[3], region[1], region[2]])
    gdal.Grid('{}.tif'.format(wg['name']), ds, options = gd_opts)
    ds = None
    gdal_set_nodata('{}.tif'.format(wg['name']), -9999)
    return(0, 0)

def waffles_invdst(wg = _waffles_grid_info, power = 2.0, smoothing = 0.0, radius1 = None, radius2 = None, angle = 0.0, \
                   max_points = 0, min_points = 0, nodata = -9999):
    '''Generate an inverse distance grid with GDAL'''
    
    radius1 = wg['inc'] * 2 if radius1 is None else gmt_inc2inc(radius1)
    radius2 = wg['inc'] * 2 if radius2 is None else gmt_inc2inc(radius2)
    gg_mod = 'invdist:power={}:smoothing={}:radius1={}:radius2={}:angle={}:max_points={}:min_points={}:nodata={}'\
                             .format(power, smoothing, radius1, radius2, angle, max_points, min_points, nodata)
    return(waffles_gdal_grid(wg, gg_mod))

def waffles_moving_average(wg = _waffles_grid_info, radius1 = None, radius2 = None, angle = 0.0, min_points = 0, nodata = -9999):
    '''generate a moving average grid with GDAL'''
    
    radius1 = wg['inc'] * 2 if radius1 is None else gmt_inc2inc(radius1)
    radius2 = wg['inc'] * 2 if radius2 is None else gmt_inc2inc(radius2)
    gg_mod = 'average:radius1={}:radius2={}:angle={}:min_points={}:nodata={}'.format(radius1, radius2, angle, min_points, nodata)
    return(waffles_gdal_grid(wg, gg_mod))

def waffles_vdatum(wg = _waffles_grid_info, ivert = 'navd88', overt = 'mhw', region = '3', jar = None):
    '''generate a 'conversion-grid' with vdatum.
    output will be the differences (surfaced) between 
    `ivert` and `overt` for the region'''
    
    vc = _vd_config
    if jar is None:
        vc['jar'] = vdatum_locate_jar()[0]
    else: vc['jar'] = jar
    vc['ivert'] = ivert
    vc['overt'] = overt
    vc['region'] = region

    gdal_null('empty.tif', waffles_proc_region(wg), 0.00083333, nodata = 0)
    with open('empty.xyz', 'w') as mt_xyz:
        gdal_dump_entry(['empty.tif', 200, 1], dst_port = mt_xyz)
    
    run_vdatum('empty.xyz', vc)
    
    if os.path.exists('result/empty.xyz') and os.stat('result/empty.xyz').st_size != 0:
        with open('result/empty.xyz') as infile:
            empty_infos = xyz_inf(infile)
        print(empty_infos)

        ll = 'd' if empty_infos[4] < 0 else '0'
        lu = 'd' if empty_infos[5] > 0 else '0'
        wg['datalists'] = ['result/empty.xyz']
        wg['spat'] = False
        wg = waffles_dict2wg(wg)
        out, status = waffles_gmt_surface(wg, tension = 0, upper_limit = lu, lower_limit = ll)
        
    remove_glob('empty.*')
    remove_glob('result/*')
    remove_glob('.mjr.datalist')
    os.removedirs('result')
    return(out, status)

_unc_config = {
    'wg': waffles_config(),
    'dem': None,
    'msk': None,
    'prox': None,
    'slp': None,
    'percentile': 95,
    'zones': ['bathy', 'bathy-topo', 'topo'],
    'sims': 10,
    'chnk_lvl': 4,
}

waffles_unc_config = lambda: copy.deepcopy(_unc_config)

def waffles_interpolation_uncertainty(uc = _unc_config):
    '''calculate the interpolation uncertainty
    - as related to distance to nearest measurement.

    returns [[err, dist] ...]'''
    s_dp = None
    s_ds = None
    echo_msg('running INTERPOLATION uncertainty module using {}...'.format(uc['wg']['mod']))
    out, status = run_cmd('gmt gmtset IO_COL_SEPARATOR = SPACE', verbose = False)
    
    ## ==============================================
    ## region analysis
    ## ==============================================
    region_info = {}
        
    ## mask analysis
    num_sum, g_max, num_perc = gdal_mask_analysis(mask = uc['msk'])

    ## proximity analysis
    prox_perc_95 = gdal_percentile(uc['prox'], 95)
    prox_perc_90 = gdal_percentile(uc['prox'], 90)
    prox_percentile = gdal_percentile(uc['prox'], uc['percentile'])

    region_info[uc['wg']['name']] = [uc['wg']['region'], g_max, num_sum, num_perc, prox_percentile] 
    for x in region_info.keys():
        echo_msg('region: {}: {}'.format(x, region_info[x]))

    ## ==============================================
    ## chunk region into sub regions
    ## ==============================================
    echo_msg('chunking region into sub-regions using chunk level {}...'.format(uc['chnk_lvl']))
    chnk_inc = int(region_info[uc['wg']['name']][4] * uc['chnk_lvl'])
    sub_regions = region_chunk(uc['wg']['region'], uc['wg']['inc'], chnk_inc)
    echo_msg('chunked region into {} sub-regions.'.format(len(sub_regions)))

    ## ==============================================
    ## sub-region analysis
    ## ==============================================
    echo_msg('analyzing {} sub-regions...'.format(len(sub_regions)))
    sub_zones = {}    
    for sc, sub_region in enumerate(sub_regions):
        gdal_cut(uc['msk'], sub_region, 'tmp_msk.tif')
        gdal_cut(uc['dem'], sub_region, 'tmp_dem.tif')
        s_sum, s_g_max, s_perc = gdal_mask_analysis('tmp_msk.tif')
        s_dc = gdal_infos('tmp_dem.tif', True)
        zone = 'Bathy' if s_dc['zr'][1] < 0 else 'Topo' if s_dc['zr'][0] > 0 else 'BathyTopo'
        sub_zones[sc + 1] = [sub_region, s_g_max, s_sum, s_perc, s_dc['zr'][0], s_dc['zr'][1], zone]
        remove_glob('tmp_*.tif')
        
    s_dens = np.array([sub_zones[x][3] for x in sub_zones.keys()])
    s_5perc = np.percentile(s_dens, 5)
    s_dens = None
    echo_msg('Sampling density for region is: {:.16f}'.format(s_5perc))

    ## ==============================================
    ## zone analysis / generate training regions
    ## ==============================================
    trainers = []
    bathy_tiles = [sub_zones[x] for x in sub_zones.keys() if sub_zones[x][6] == 'Bathy']
    bathy_topo_tiles = [sub_zones[x] for x in sub_zones.keys() if sub_zones[x][6] == 'BathyTopo']
    topo_tiles = [sub_zones[x] for x in sub_zones.keys() if sub_zones[x][6] == 'Topo']

    for z, tile_set in enumerate([bathy_tiles, bathy_topo_tiles, topo_tiles]):
        if len(tile_set) > 0:
            t_dens = np.array([x[3] for x in tile_set])
            t_50perc = np.percentile(t_dens, 50)
        else: t_50perc = 0.0
        echo_msg('Minimum sampling for {} tiles: {}'.format(uc['zones'][z].upper(), t_50perc))
        t_trainers = [x for x in tile_set if x[3] > t_50perc]
        echo_msg('possible {} training zones: {}'.format(uc['zones'][z].upper(), len(t_trainers)))
        trainers.append(t_trainers)
    trains = regions_sort(trainers)
    echo_msg('sorted training tiles.')
    echo_msg('analyzed {} sub-regions.'.format(len(sub_regions)))

    ## ==============================================
    ## split-sample simulations and error calculations
    ## ==============================================
    for sim in range(0, uc['sims']):
        sys.stderr.write('\x1b[2K\rwaffles: performing SPLIT-SAMPLE simulation {} out of {} [{:3}%]'.format(sim + 1, uc['sims'], 0))
        sys.stderr.flush()
        status = 0
        for z, train in enumerate(trains):
            train_h = train[:25]
            ss_samp = s_5perc

            ## ==============================================
            ## perform split-sample analysis on each training region.
            ## ==============================================
            for n, sub_region in enumerate(train_h):
                perc = int(float(n+(len(train_h) * z))/(len(train_h)*len(trains)) * 100)
                sys.stderr.write('\x1b[2K\rwaffles: performing SPLIT-SAMPLE simulation {} out of {} [{:3}%]'.format(sim + 1, uc['sims'], perc))
                this_region = sub_region[0]
                if sub_region[3] < ss_samp: ss_samp = None

                ## ==============================================
                ## extract the xyz data for the region from the DEM
                ## ==============================================
                o_xyz = '{}_{}.xyz'.format(uc['wg']['name'], n)
                ds = gdal.Open(uc['dem'])
                with open(o_xyz, 'w') as o_fh:
                    for xyz in gdal_parse(ds, srcwin = gdal_srcwin(ds, region_buffer(this_region, (20 * uc['wg']['inc']))), mask = uc['msk']):
                        xyz_line(xyz, o_fh)
                ds = None

                if os.stat(o_xyz).st_size == 0:
                    echo_error_msg('no data in sub-region...')
                else:
                    ## ==============================================
                    ## split the xyz data to inner/outer; outer is
                    ## the data buffer, inner will be randomly sampled
                    ## ==============================================
                    s_inner, s_outer = gmt_select_split(o_xyz, this_region, 'sub_{}'.format(n), verbose = False) #verbose = uc['wg']['verbose'])
                    if os.stat(s_inner).st_size != 0:
                        sub_xyz = np.loadtxt(s_inner, ndmin=2, delimiter = ' ')
                    else: sub_xyz = []
                    ss_len = len(sub_xyz)
                    if ss_samp is not None:
                        sx_cnt = int(sub_region[1] * (ss_samp / 100)) + 1
                    else: sx_cnt = 1
                    sub_xyz_head = 'sub_{}_head.xyz'.format(n)
                    np.random.shuffle(sub_xyz)
                    np.savetxt(sub_xyz_head, sub_xyz[:sx_cnt], '%f', ' ')

                    ## ==============================================
                    ## generate the random-sample DEM
                    ## ==============================================
                    wc = waffles_config()
                    wc['name'] = 'sub_{}'.format(n)
                    wc['datalists'] = [s_outer, sub_xyz_head]
                    wc['region'] = this_region
                    wc['inc'] = uc['wg']['inc']
                    wc['mod'] = uc['wg']['mod']
                    wc['verbose'] = False
                    wc['mod_args'] = uc['wg']['mod_args']
                    wc['mask'] = True
                    sub_dem = waffles_run(wc)
                    sub_msk = '{}_msk.tif'.format(wc['name'])
                    
                    if os.path.exists(sub_dem) and os.path.exists(sub_msk):
                        ## ==============================================
                        ## generate the random-sample data PROX and SLOPE
                        ## ==============================================        
                        sub_prox = '{}_prox.tif'.format(wc['name'])
                        gdal_proximity(sub_msk, sub_prox)

                        sub_slp = '{}_slp.tif'.format(wc['name'])
                        gdal_slope(sub_dem, sub_slp)

                        ## ==============================================
                        ## Calculate the random-sample errors
                        ## ==============================================
                        sub_xyd = gdal_query(sub_xyz[sx_cnt:], sub_dem, 'xyd')
                        #sub_dp = gdal_query(sub_xyd, sub_prox, 'zg')
                        sub_dp = gdal_query(sub_xyd, sub_prox, 'xyzg')
                        sub_ds = gdal_query(sub_dp, uc['slp'], 'g')
 
                        if len(sub_dp) > 0:
                            if sub_dp.shape[0] == sub_ds.shape[0]:
                                sub_dp = np.append(sub_dp, sub_ds, 1)
                            else:
                                print(n)
                                print(sub_dp.shape)
                                print(sub_ds.shape)
                                sub_dp = []
                    else: sub_dp = None
                    remove_glob(sub_xyz_head)

                    if s_dp is not None: 
                        if sub_dp is not None and len(sub_dp) > 0:
                            s_dp = np.concatenate((s_dp, sub_dp), axis = 0)
                    else: s_dp = sub_dp
                remove_glob(o_xyz)
                remove_glob('sub_{}*'.format(n))
    echo_msg('ran INTERPOLATION uncertainty module using {}.'.format(uc['wg']['mod']))

    if len(s_dp) > 0:
        ## ==============================================
        ## save err dist data files
        ## ==============================================
        echo_msg('gathered {} error points'.format(len(s_dp)))

        #np.savetxt('{}.err'.format(uc['wg']['name']), s_dp, '%f', ' ')

        d_max = region_info[uc['wg']['name']][4]
        s_dp = s_dp[s_dp[:,3] < d_max,:]

        prox_err = s_dp[:,[2,3]]
        slp_err = s_dp[:,[2,4]]
        
        np.savetxt('{}_prox.err'.format(uc['wg']['name']), prox_err, '%f', ' ')
        np.savetxt('{}_slp.err'.format(uc['wg']['name']), slp_err, '%f', ' ')

        ec_d = err2coeff(prox_err[:50000000], dst_name = uc['wg']['name'] + '_prox', xa = 'distance')
        ec_s = err2coeff(slp_err[:50000000], dst_name = uc['wg']['name'] + '_slp', xa = 'slope')

        ## ==============================================
        ## apply error coefficient to full proximity grid
        ## ==============================================
        echo_msg('applying coefficient to proximity grid')
        ## USE numpy instead
        math_cmd = 'gmt grdmath {} 0 AND ABS {} POW {} MUL {} ADD = {}_prox_unc.tif=gd+n-9999:GTiff\
        '.format(uc['prox'], ec_d[2], ec_d[1], 0, uc['wg']['name'])
        run_cmd(math_cmd, verbose = uc['wg']['verbose'])
        echo_msg('applied coefficient {} to proximity grid'.format(ec_d))
        
        math_cmd = 'gmt grdmath {} 0 AND ABS {} POW {} MUL {} ADD = {}_slp_unc.tif=gd+n-9999:GTiff\
        '.format(uc['slp'], ec_s[2], ec_s[1], 0, uc['wg']['name'])
        run_cmd(math_cmd, verbose = uc['wg']['verbose'])
        echo_msg('applied coefficient {} to slope grid'.format(ec_s))
        
    return([ec_d, ec_s])

def waffles_wg_valid_p(wg = _waffles_grid_info):
    '''return True if wg_config appears valid'''
    
    if wg['datalists'] is None: return(False)
    if wg['mod'] is None: return(False)
    else: return(True)
        
def waffles_gdal_md(wg):
    '''add metadata to the waffles dem'''
    
    ds = gdal.Open('{}.tif'.format(wg['name']), gdal.GA_Update)
    if ds is not None:
        md = ds.GetMetadata()
        if wg['node'] == 'pixel':
            md['AREA_OR_POINT'] = 'Area'
        else: md['AREA_OR_POINT'] = 'Point'
        ds.SetMetadata(md)
        ds = None
    else: echo_error_msg('failed to set metadata')

def waffles_run(wg = _waffles_grid_info):
    '''generate a DEM using wg dict settings
    see waffles_dict2wg() to generate a wg config.

    - runs the waffles module to generate the DEM
    - optionally clips the output to shapefile
    - optionally filters the output
    - optionally resamples the output
    - cuts the output to dist-size
    - reformats the output to final format
    - sets metadata in output

    returns dem-fn'''

    out, status = run_cmd('gmt gmtset IO_COL_SEPARATOR = SPACE', verbose = False)
    
    ## ==============================================
    ## validate and/or set the waffles_config
    ## ==============================================
    wg = waffles_dict2wg(wg)
    if wg is None:
        #echo_error_msg('invalid configuration, {}'.format(json.dumps(wg, indent=4, sort_keys=True)))
        echo_error_msg('invalid configuration, {}'.format(wg))
        sys.exit(-1)

    args_d = {}
    args_d = args2dict(wg['mod_args'], args_d)
    if wg['verbose']:
        echo_msg(wg)
        #echo_msg(json.dumps(wg, indent = 4, sort_keys = True))
        echo_msg(wg['datalist'])
        echo_msg('running module {} with {} [{}]...'.format(wg['mod'], wg['mod_args'], args_d))

    dem = '{}.tif'.format(wg['name'])
    if wg['mask']: dem_msk = '{}_msk.tif'.format(wg['name'])
    if wg['spat']: dem_spat = '{}_sm.shp'.format(wg['name'])

    ## ==============================================
    ## optionally generate the DEM in chunks
    ## ==============================================
    if wg['chunk'] is not None:
        xcount, ycount, dst_gt = gdal_region2gt(wg['region'], wg['inc'])
        s_regions = region_chunk(wg['region'], wg['inc'], (xcount/wg['chunk'])+1)
    else: s_regions = [wg['region']]

    chunks = []
    if wg['mask']: chunks_msk = []
    if wg['spat']: chunks_spat = []
    for region in s_regions:
        this_wg = waffles_dict2wg(wg)
        this_wg['region'] = region
        this_wg['name'] = 'chunk_{}'.format(region_format(region, 'fn'))
        this_dem = this_wg['name'] + '.tif'
        chunks.append(this_dem)
        if this_wg['mask']:
            this_dem_msk = this_wg['name'] + '_msk.tif'
            chunks_msk.append(this_dem_msk)
        if this_wg['spat']: chunks_spat.append('{}_sm.shp'.format(this_wg['name']))
        args_d['wg'] = this_wg

        ## ==============================================
        ## gererate the DEM (run the module)
        ## ==============================================
        #try:
        out, status = _waffles_modules[this_wg['mod']][0](args_d)
        #except KeyboardInterrupt as e:
        #    echo_error_msg('killed by user, {}'.format(e))
        #    sys.exit(-1)
        #except Exception as e:
        #    echo_error_msg('{}'.format(e))
        #    status = -1

        if status != 0: remove_glob(this_dem)
        if not os.path.exists(this_dem): continue
        gdi = gdal_infos(this_dem, scan = True)
        if gdi is not None:
            if np.isnan(gdi['zr'][0]):
                remove_glob(this_dem)
                if this_wg['mask']: remove_glob(this_dem_msk)
                continue
        else: continue

        gdal_set_epsg(this_dem, this_wg['epsg'])
        waffles_gdal_md(this_wg)

        ## ==============================================
        ## optionally clip the DEM to polygon
        ## ==============================================
        if this_wg['clip'] is not None:
            if this_wg['verbose']: echo_msg('clipping {}...'.format(this_dem))
            clip_args = {}
            cp = this_wg['clip'].split(':')
            clip_args['src_ply'] = cp[0]
            clip_args = args2dict(cp[1:], clip_args)
            gdal_clip(this_dem, **clip_args)
            if this_wg['mask']:
                if this_wg['verbose']: echo_msg('clipping {}...'.format(this_dem_msk))
                gdal_clip(this_dem_msk, **clip_args)

        if not os.path.exists(this_dem): continue
        gdi = gdal_infos(this_dem, scan = True)
        if gdi is not None:
            if np.isnan(gdi['zr'][0]):
                remove_glob(this_dem)
                if this_wg['mask']: remove_glob(this_dem_msk)
                continue
        else: continue
                
        ## ==============================================
        ## optionally filter the DEM 
        ## ==============================================
        if this_wg['fltr'] is not None:
            if this_wg['verbose']: echo_msg('filtering {}...'.format(this_dem))
            fltr_args = {}
            fltr = this_wg['fltr'].split(':')
            fltr_args['fltr'] = gmt_inc2inc(fltr[0])
            fltr_args['use_gmt'] = True
            fltr_args = args2dict(fltr[1:], fltr_args)        
            if fltr_args['use_gmt']: fltr_args['use_gmt'] = True if this_wg['gc']['GMT'] is not None else False
            try:
                gdal_smooth(this_dem, 'tmp_s.tif', **fltr_args)
                os.rename('tmp_s.tif', this_dem)
            except TypeError as e: echo_error_msg('{}'.format(e))

        ## ==============================================
        ## optionally resample the DEM 
        ## ==============================================
        if this_wg['sample'] is not None:
            if this_wg['verbose']: echo_msg('resampling {}...'.format(this_dem))
            if this_wg['gc']['GMT'] is not None:
                gmt_sample_inc(this_dem, inc = this_wg['sample'], verbose = this_wg['verbose'])
                if this_wg['mask']:
                    if this_wg['verbose']: echo_msg('resampling {}...'.format(this_dem_msk))
                    gmt_sample_inc(this_dem_msk, inc = this_wg['sample'], verbose = this_wg['verbose'])
            else:
                out, status = run_cmd('gdalwarp -tr {:.10f} {:.10f} {} -r bilinear -te {} tmp.tif\
                '.format(inc, inc, src_grd, region_format(waffles_proc_region(this_wg)), verbose = verbose))
                if status == 0: os.rename('tmp.tif', '{}'.format(src_grd))

        ## ==============================================
        ## cut dem to final size - region buffered by (inc * extend)
        ## ==============================================
        #try:
        out = gdal_cut(this_dem, waffles_dist_region(this_wg), 'tmp_cut.tif')
        if out is not None: os.rename('tmp_cut.tif', this_dem)
        if this_wg['mask']:
            out = gdal_cut(this_dem_msk, waffles_dist_region(this_wg), 'tmp_cut.tif')
            if out is not None: os.rename('tmp_cut.tif', this_dem_msk)
        #except OSError as e:
        #    remove_glob('tmp_cut.tif')
        #    echo_error_msg('cut failed, is the dem open somewhere, {}'.format(e))                
                
    ## ==============================================
    ## merge the chunks and remove
    ## ==============================================
    if len(chunks) > 1:
        out, status = run_cmd('gdal_merge.py -n -9999 -a_nodata -9999 -ps {} -{} -ul_lr {} -o {} {}\
        '.format(wg['inc'], wg['inc'], waffles_dist_ul_lr(wg), dem, ' '.join(chunks)), verbose = True)
        ## add option to keep chunks.
        [remove_glob(x) for x in chunks]            
    else:
        if os.path.exists(chunks[0]):
            os.rename(chunks[0], dem)

    if this_wg['mask']:
        if len(chunks_msk) > 1:
            out, status = run_cmd('gdal_merge.py -n -9999 -a_nodata -9999 -ps {} -{} -ul_lr {} -o {} {}\
            '.format(wg['inc'], wg['inc'], waffles_dist_ul_lr(wg), dem_msk, ' '.join(chunks_msk)), verbose = True)
            ## add option to keep chunks.
            [remove_glob(x) for x in chunks_msk]
        else:
            if os.path.exists(chunks_msk[0]):
                os.rename(chunks_msk[0], dem_msk)

    if this_wg['spat']:
        if len(chunks_spat) > 1:
            out, status = run_cmd('ogrmerge.py {} {}'.format(dem_spat, ' '.join(chunks_spat)))
            [remove_glob('{}*'.format(x.split('.')[0])) for x in chunks_spat]
        else:
            out, status = run_cmd('ogr2ogr {} {}'.format(dem_spat, chunks_spat[0]))
            remove_glob('{}*'.format(chunks_spat[0].split('.')[0]))
                
    if os.path.exists(dem):
        ## ==============================================
        ## convert to final format
        ## ==============================================
        if wg['fmt'] != 'GTiff':
            orig_dem = dem
            if wg['gc']['GMT'] is not None:
                dem = gmt_grd2gdal(dem, wg['fmt'])
            else: dem = gdal_gdal2gdal(dem, wg['fmt'])
            remove_glob(orig_dem)

        ## ==============================================
        ## set the projection and other metadata
        ## ==============================================
        gdal_set_epsg(dem, wg['epsg'])
        waffles_gdal_md(wg)
        if wg['mask']: gdal_set_epsg(dem_msk, wg['epsg'])

    ## ==============================================
    ## optionally generate uncertainty grid
    ## ==============================================
    if wg['unc']:
        try:
            if os.path.exists(dem) and os.path.exists(dem_msk):
                echo_msg('generating uncertainty')

                uc = _unc_config
                uc['wg'] = wg
                uc['dem'] = dem
                uc['msk'] = dem_msk

                dem_prox = '{}_prox.tif'.format(wg['name'])
                gdal_proximity(dem_msk, dem_prox)
                uc['prox'] = dem_prox

                dem_slp = '{}_slp.tif'.format(wg['name'])
                gdal_slope(dem, dem_slp)
                uc['slp'] = dem_slp

                echo_msg(uc)
                waffles_interpolation_uncertainty(uc)
        except Exception as e:
            echo_error_msg('failed to calculate uncertainty, {}'.format(e))

    ## ==============================================
    ## if dem has data, return
    ## ==============================================
    remove_glob('waffles_dem_mjr.datalist')
    remove_glob(wg['datalist'])
    return(dem)
        
## ==============================================
## waffles cli
## ==============================================
waffles_cli_usage = '''waffles [OPTIONS] <datalist/entry>

Generate DEMs and derivatives and process datalists.

General Options:
  -R, --region\t\tSpecifies the desired REGION;
\t\t\tThis can either be a GMT-style region ( -R xmin/xmax/ymin/ymax )
\t\t\tor an OGR-compatible vector file with regional polygons. 
\t\t\tIf a vector file is supplied it will search each region found therein.
\t\t\tIf omitted, use the region gathered from the data in DATALIST.
  -E, --increment\tGridding CELL-SIZE in native units or GMT-style increments.
\t\t\tappend :<inc> to resample the output to the given <inc>: -E.3333333s:.1111111s
  -F, --format\t\tOutput grid FORMAT. [GTiff]
  -M, --module\t\tDesired DEM MODULE and options. (see available Modules below)
\t\t\tsyntax is -M module:mod_opt=mod_val:mod_opt1=mod_val1:...
  -O, --output-name\tBASENAME for all outputs.
  -P, --epsg\t\tHorizontal projection of data as EPSG code [4326]
  -X, --extend\t\tNumber of cells with which to EXTEND the REGION.
\t\t\tappend :<num> to extend the processing region: -X6:12
  -T, --filter\t\tFILTER the output using a Cosine Arch Filter at -T<dist(km)> search distance.
\t\t\tIf GMT is not available, or if :use_gmt=False, perform a Gaussian filter at -T<factor>. 
\t\t\tAppend :split_value=<num> to only filter values below <num>.
\t\t\te.g. -T10:split_value=0:use_gmt=False to smooth bathymetry using Gaussian filter
  -Z --z-region\t\tRestrict data processing to records that fall within the z-region
\t\t\tUse '-' to indicate no bounding range; e.g. -Z-/0 will restrict processing to data
\t\t\trecords whose z value is below zero.
  -C, --clip\t\tCLIP the output to the clip polygon -C<clip_ply.shp:invert=False>
  -K, --chunk\t\tProcess the region in CHUNKs. -K<chunk-level>
  -W, --wg-config\tA waffles config JSON file. If supplied, will overwrite all other options.
\t\t\tgenerate a waffles_config JSON file using the --config flag.

  -p, --prefix\t\tSet BASENAME to PREFIX (append inc/region/year info to output BASENAME).
  -r, --grid-node\tUse grid-node registration, default is pixel-node
  -w, --weights\t\tUse weights provided in the datalist to weight overlapping data.

  -a, --archive\t\tArchive the datalist to the given region.
  -m, --mask\t\tGenerate a data mask raster.
  -s, --spat-meta\tGenerate spatial-metadata.
  -u, --uncert\t\tGenerate uncertainty grid.

  --help\t\tPrint the usage text
  --config\t\tSave the waffles config JSON and major datalist
  --modules\t\tDisply the module descriptions and usage
  --version\t\tPrint the version information
  --verbose\t\tIncrease the verbosity

Datalists and data formats:
  A datalist is a file that contains a number of datalist entries, while an entry is a space-delineated line:
  `/path/to/data format weight data,meta,data`

Supported datalist formats: 
  {}

Modules (see waffles --modules for more info):
  {}

CIRES DEM home page: <http://ciresgroups.colorado.edu/coastalDEM>
'''.format(_known_datalist_fmts_short_desc(), _waffles_module_short_desc(_waffles_modules))

def waffles_cli(argv = sys.argv):
    '''run waffles from command-line
    e.g. `python waffles.py` 
    generates a waffles_config from the command-line options
    and either outputs the or runs the waffles_config
    on each region supplied (multiple regions can be supplied
    by using a vector file as the -R option.)
    See `waffles_cli_usage` for full cli options.'''
    
    wg = waffles_config()
    wg_user = None
    dls = []
    region = None
    module = None
    want_prefix = False
    want_verbose = False
    want_config = False
    status = 0
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == '--region' or arg == '-R':
            region = str(argv[i + 1])
            i += 1
        elif arg[:2] == '-R': region = str(arg[2:])
        elif arg == '--module' or arg == '-M':
            module = str(argv[i + 1])
            i += 1
        elif arg[:2] == '-M': module = str(arg[2:])
        elif arg == '--increment' or arg == '-E':
            incs = argv[i + 1].split(':')
            wg['inc'] = gmt_inc2inc(incs[0])
            if len(incs) > 1: wg['sample'] = gmt_inc2inc(incs[1])
            i = i + 1
        elif arg[:2] == '-E':
            incs = arg[2:].split(':')
            wg['inc'] = gmt_inc2inc(arg[2:].split(':')[0])
            if len(incs) > 1: wg['sample'] = gmt_inc2inc(incs[1])
        elif arg == '--outname' or arg == '-O':
            wg['name'] = argv[i + 1]
            i += 1
        elif arg[:2] == '-O': wg['name'] = arg[2:]
        elif arg == '--format' or arg == '-F':
            wg['fmt'] = argv[i + 1]
            i += 1
        elif arg[:2] == '-F': wg['fmt'] = arg[2:]
        elif arg == '--filter' or arg == '-T':
            wg['fltr'] = argv[i + 1]
            i += 1
        elif arg[:2] == '-T': wg['fltr'] = arg[2:]
        elif arg == '--extend' or arg == '-X':
            exts = argv[i + 1].split(':')
            wg['extend'] = exts[0]
            if len(exts) > 1: wg['extend_proc'] = exts[1]
            i += 1
        elif arg[:2] == '-X':
            exts = arg[2:].split(':')
            wg['extend'] = exts[0]
            if len(exts) > 1: wg['extend_proc'] = exts[1]
        elif arg == '--wg-config' or arg == '-W':
            wg_user = argv[i + 1]
            i += 1
        elif arg[:2] == '-W': wg_user = arg[2:]
        elif arg == '--clip' or arg == '-C':
            wg['clip'] = argv[i + 1]
            i = i + 1
        elif arg[:2] == '-C': wg['clip'] = arg[2:]
        elif arg == '--chunk' or arg == '-K':
            wg['chunk'] = argv[i + 1]
            i = i + 1
        elif arg[:2] == '-K': wg['chunk'] = arg[2:]
        elif arg == '--epsg' or arg == '-P':
            wg['epsg'] = argv[i + 1]
            i = i + 1
        elif arg[:2] == '-P': wg['epsg'] = arg[2:]
        elif arg == '--z-range' or arg == '-Z':
            zr = argv[i + 1].split('/')
            if len(zr) > 1:
                wg['lower_limit'] = None if zr[0] == '-' else float(zr[0])
                wg['upper_limit'] = None if zr[1] == '-' else float(zr[1])
            i = i + 1
        elif arg[:2] == '-Z':
            zr = arg[2:].split('/')
            if len(zr) > 1:
                wg['lower_limit'] = None if zr[0] == '-' else float(zr[0])
                wg['upper_limit'] = None if zr[1] == '-' else float(zr[1])
        elif arg == '-w' or arg == '--weights': wg['weights'] = True
        elif arg == '-p' or arg == '--prefix': want_prefix = True
        elif arg == '-a' or arg == '--archive': wg['archive'] = True
        elif arg == '-m' or arg == '--mask': wg['mask'] = True
        elif arg == '-u' or arg == '--uncert':
            wg['mask'] = True
            wg['unc'] = True
        elif arg == '-s' or arg == 'spat-meta': wg['spat'] = True
        elif arg == '-r' or arg == '--grid-node': wg['node'] = 'grid'
        elif arg == '--verbose' or arg == '-V': wg['verbose'] = True
        elif arg == '--config': want_config = True
        elif arg == '--modules' or arg == '-m':
            sys.stderr.write(_waffles_module_long_desc(_waffles_modules))
            sys.exit(0)
        elif arg == '--help' or arg == '-h':
            sys.stderr.write(waffles_cli_usage)
            sys.exit(0)
        elif arg == '--version' or arg == '-v':
            sys.stdout.write('{}\n'.format(_version))
            sys.exit(0)
        else: dls.append(arg)
        i += 1

    ## ==============================================
    ## load the user wg json and run waffles with that.
    ## ==============================================
    if wg_user is not None:
        if os.path.exists(wg_user):
            try:
                with open(wg_user, 'r') as wgj:
                    wg = json.load(wgj)
                    dem = waffles_run(wg)
                    sys.exit(0)
            except Exception as e:
                wg = waffles_config()
                echo_error_msg(e)
        else:
            echo_error_msg('specified json file does not exist, {}'.format(wg_user))
            sys.exit(0)

    ## ==============================================
    ## Otherwise run from cli options...
    ## set the dem module
    ## ==============================================
    if module is not None:
        mod_opts = {}
        opts = module.split(':')
        if opts[0] in _waffles_modules.keys():
            mod_opts[opts[0]] = list(opts[1:])
        else: echo_error_msg('invalid module name `{}`'.format(opts[0]))
        
        for key in mod_opts.keys():
            mod_opts[key] = [None if x == '' else x for x in mod_opts[key]]
        mod = opts[0]
        mod_args = tuple(mod_opts[mod])
        wg['mod'] = mod
        wg['mod_args'] = mod_args

    if wg['mod'] != 'vdatum':
        if len(dls) == 0:
            sys.stderr.write(waffles_cli_usage)
            echo_error_msg('''must specify a datalist/entry, try gmrt or srtm for global data.''')
            sys.exit(-1)
            
    ## ==============================================
    ## set the datalists and names
    ## ==============================================
    wg['datalists'] = dls
    
    ## ==============================================
    ## reformat and set the region
    ## ==============================================
    if region is not None:
        try:
            these_regions = [[float(x) for x in region.split('/')]]
        except ValueError: these_regions = gdal_ogr_regions(region)
        except Exception as e:
            echo_error_msg('failed to parse region(s), {}'.format(e))
    else: these_regions = [None]
    if len(these_regions) == 0: echo_error_msg('failed to parse region(s), {}'.format(region))
    if want_prefix or len(these_regions) > 1: wg['name_prefix'] = wg['name']
    
    ## ==============================================
    ## run waffles for each input region.
    ## ==============================================
    bn = wg['name']
    for this_region in these_regions:
        wg['region'] = this_region
        
        if want_config:
            this_wg = waffles_dict2wg(wg)
            if this_wg is not None:
                #echo_msg(json.dumps(this_wg, indent = 4, sort_keys = True))
                echo_msg(this_wg)
                with open('{}.json'.format(this_wg['name']), 'w') as wg_json:
                    echo_msg('generating waffles config file: {}.json'.format(this_wg['name']))
                    echo_msg('generating major datalist: {}_mjr.datalist'.format(this_wg['name']))
                    wg_json.write(json.dumps(this_wg, indent = 4, sort_keys = True))
            else: echo_error_msg('could not parse config.')
        else:
            ## ==============================================
            ## generate the DEM
            ## ==============================================
            # import threading
            # t = threading.Thread(target = waffles_run, args = (wg,))
            # p = _progress('waffles: generating dem')
            # try:
            #     t.start()
            #     while True:
            #         time.sleep(1)
            #         p.update()
            #         if not t.is_alive():
            #             break
            # except (KeyboardInterrupt, SystemExit):
            #     echo_msg('stopping all threads')
            #     stop_threads = True
            #     status = -1
            
            # t.join()
            # p.end(status, 'waffles: generated dem')
            #try:
            em = waffles_run(wg)
            #except RuntimeError or OSError as e:
            #    echo_error_msg('Cannot access {}.tif, may be in use elsewhere, {}'.format(wg['name'], e))

## ==============================================
## mainline -- run waffles directly...
##
## run waffles:
## % python waffles.py dem <args>
## % python waffles.py <args>
##
## run datalists:
## % python waffles.py datalists <args>
## ==============================================
if __name__ == '__main__': waffles_cli(sys.argv)

### End
