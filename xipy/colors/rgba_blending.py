# NumPy
import numpy as np
from scipy import ndimage

# Matplotlib
import matplotlib as mpl
from matplotlib.colors import Normalize

# Enthought Traits
import enthought.traits.api as t_ui
import enthought.traits.ui.api as ui_api

# NIPY
import nipy.core.api as ni_api

# XIPY
from _blend_pix import resample_and_blend, resize_lookup_array
from _blend_pix import *
import xipy.colors.color_mapping as cm
import xipy.volume_utils as vu
from xipy.slicing.image_slicers import ResampledIndexVolumeSlicer, \
     SAG, COR, AXI, xipy_ras

def blend_two_images(base_img, base_cmap, base_alpha,
                     over_img, over_cmap, over_alpha):
    """ Taking two ResampledVolumeSlicers and corresponding colormaps,
    blend the two images into an RGBA byte array shaped like the resampled
    image of base_img.

    Parameters
    ----------
    base_img : ResampledVolumeSlicer
        The underlying image
    base_cmap : matplotlib.colors.LinearSegmentedColormap
        The mapping function to convert base_img's scalars to RGBA colors
    base_alpha : scalar or len-256 iterable (optional)
        the strength of this map when alpha blending;
        scalar in range [0,1], iterable should be ints from [0,255]
    over_img : ResampledVolumeSlicer
        The overlying image
    over_cmap : matplotlib.colors.LinearSegmentedColormap
        The mapping function to convert over_img's scalars to RGBA colors
    over_alpha : scalar or len-256 iterable (optional)
        the strength of this map when alpha blending;
        scalar in range [0,1], iterable should be ints from [0,255]

    Returns
    -------
    blended_arr : ndarray, dtype=uint8
        The blended RGBA array
    """
    base_bytes = normalize_and_map(base_img.image_arr, base_cmap,
                                   alpha=base_alpha)
    base_spacing = base_img.grid_spacing
    base_origin = np.array(base_img.bbox)[:,0]
    over_bytes = normalize_and_map(over_img.image_arr, over_cmap,
                                   alpha=over_alpha)
    over_spacing = over_img.grid_spacing
    over_origin = np.array(over_img.bbox)[:,0]
    resample_and_blend(base_bytes, base_spacing, base_origin,
                       over_bytes, over_spacing, over_origin)
    return base_bytes


def normalize_and_map(arr, cmap, alpha=1, norm_min=None, norm_max=None):
    """ Taking a scalar array, normalize the array to the range [0,1],
    and map the values through cmap into RGBA bytes

    Parameters
    ----------
    arr : ndarray
        array to map
    cmap : xipy.colors.color_mapping.MixedAlphaColormap
        the mapping function
    alpha : scalar or len-256 iterable (optional)
        the strength of this map when alpha blending;
        scalar in range [0,1], iterable should be ints from [0,255]
    norm_min : scalar (optional)
        the value to rescale to zero (possibly clipping the transformed array)
    norm_max : scalar (optional)
        the value to rescale to one (possibly clipping the transformed array)
    """
    norm = mpl.colors.Normalize(vmin=norm_min, vmax=norm_max)
    scaled = norm(arr)
    bytes = cmap(scaled, alpha=alpha, bytes=True)
    return bytes

def quick_min_max_norm(arr):
    an = arr.copy()
    an -= np.nanmin(an)
    an /= np.nanmax(an)
    return an

def blend_helper(a1, a2):
    xyz_shape = a1.shape[:-1]
    npts = np.prod(xyz_shape)
    a1.shape = (npts, 4)
    a2.shape = (npts, 4)
    blend_same_size_arrays(a1, a2)
    a1.shape = xyz_shape + (4,)
    a2.shape = xyz_shape + (4,)


class BlendedArrays(t_ui.HasTraits):
    """
    This class can color map and blend two like-sized arrays
    of luminance values transformed into LUT indices.

    This is intended as a parent class, whose subclasses will
    provide and manager their own LUT index arrays (_main_idx and _over_idx)
    """

    # The LUT index images
    _main_idx = t_ui.Array(comparison_mode=t_ui.NO_COMPARE)
    _over_idx = t_ui.Array(comparison_mode=t_ui.NO_COMPARE)

    # the RGBA byte arrays
    main_rgba = t_ui.Array(dtype='B', comparison_mode=t_ui.NO_COMPARE)
    over_rgba = t_ui.Array(dtype='B', comparison_mode=t_ui.NO_COMPARE)
    blended_rgba = t_ui.Property(depends_on='main_rgba, over_rgba')

    # Color mapping properties
    main_cmap = t_ui.Instance(cm.MixedAlphaColormap)
    over_cmap = t_ui.Instance(cm.MixedAlphaColormap)    

    main_norm = t_ui.Tuple((0.0, 0.0))
    over_norm = t_ui.Tuple((0.0, 0.0))

    main_alpha = t_ui.Any # can be a float or array??
    over_alpha = t_ui.Any

    def __init__(self, **traits):
        t_ui.HasTraits.__init__(self, **traits)
        if not self.main_cmap:
            self.set(main_cmap=cm.gray, trait_change_notify=False)
        if not self.over_cmap:
            self.set(over_cmap=cm.jet, trait_change_notify=False)
        if self.main_alpha is None:
            self.set(main_alpha=1.0, trait_change_notify=False)
        if self.over_alpha is None:
            self.set(over_alpha=1.0, trait_change_notify=False)

    def update_main_props(self, **props):
        self._update_props(
            'main', **props
            )

    def update_over_props(self, **props):
        self._update_props(
            'over', **props
            )

    def _update_props(self, array, **props):
        pvals = (); pnames = ()
        for name in ('cmap', 'alpha', 'norm'):
            v = props.get(name)
            if v is not None:
                pvals += (v,)
                pnames += (name,)        
        if not len(pvals):
            return
        update_dict = dict(
            ( (array+'_'+prop, pval)
              for prop, pval in zip(pnames, pvals) )
            )
        if len(pvals)==1:
            self.trait_set(**update_dict)
        else:
            self.trait_setq(**update_dict)
            name = array+'_cmap'
            self._remap_index_image(name, None)
    
    def _check_alpha(self, alpha):
        if mpl.cbook.iterable(alpha):
            return np.clip(alpha, 0, 1)
        # assuming both cmaps have 256 colors!!
        return np.ones(256)*max(0.0, min(alpha, 1.0))

    @t_ui.cached_property
    def _get_blended_rgba(self):
        print 'update to blended image triggered'
        has_over = len(self.over_rgba)
        has_main = len(self.main_rgba)
        # XXX: MAYBE SHOULD FILL ALPHA CHANNEL WITH 255 WHEN NOT BLENDING
        if not has_over:
            # even return this if main_rgba is empty
            return self.main_rgba
        if has_over and not has_main:
            return self.over_rgba
        blended_rgba = self.main_rgba.copy()
        blend_helper(blended_rgba, self.over_rgba)
        return blended_rgba

    # Keep main/over RGBA values locked to the index images
    @t_ui.on_trait_change('_main_idx, _over_idx')
    def _map_rgba(self, name, new):
        if name=='_main_idx':
            self.main_rgba = self.main_cmap.fast_lookup(
                self._main_idx, alpha=self.main_alpha, bytes=True
                )
        else:
            self.over_rgba = self.over_cmap.fast_lookup(
                self._over_idx, alpha=self.over_alpha, bytes=True
                )
    
    @t_ui.on_trait_change('main_cmap, over_cmap')
    def _remap_index_image(self, name, new):
        print 'remapping', name
        if name=='main_cmap' and len(self._main_idx):
            self.main_rgba[:] = self.main_cmap.fast_lookup(
                self._main_idx, alpha=self.main_alpha, bytes=True
                )
            # have to do this explicitly to set off trait notification
            self.main_rgba = self.main_rgba
        elif len(self._over_idx):
            self.over_rgba[:] = self.over_cmap.fast_lookup(
                self._over_idx, alpha=self.over_alpha, bytes=True
                )
            self.over_rgba = self.over_rgba

    @t_ui.on_trait_change('main_alpha, over_alpha')
    def _fast_remap_alpha(self, name, changed):
        print 'remapping alpha'
        if name=='main_alpha':
            # store new alpha
            main_alpha = self._check_alpha(self.main_alpha)
            self.trait_setq(main_alpha=main_alpha)
            if len(self._main_idx):
                # if there's an image, remap it
                a_under, a_over, a_bad = self.main_cmap._lut[-3:,-1]
                alpha_lut = np.r_[main_alpha*255, a_under, a_over, a_bad]
                alpha_lut.take(self._main_idx, axis=0,
                               mode='clip', out=self.main_rgba[...,3])
                print 'looked up new main alpha chan'
                # have to do this explicitly to set off trait notification
                self.main_rgba = self.main_rgba
        elif name=='over_alpha':
            # store new alpha
            over_alpha = self._check_alpha(self.over_alpha)
            self.trait_setq(over_alpha=over_alpha)
            if len(self._over_idx):
                # if there's an image, remap it
                a_under, a_over, a_bad = self.over_cmap._lut[-3:,-1]
                alpha_lut = np.r_[over_alpha*255, a_under, a_over, a_bad]
                alpha_lut.take(self._over_idx, axis=0,
                               mode='clip', out=self.over_rgba[...,3])
                print 'looked up new over alpha chan'
                # have to do this explicitly to set off trait notification
                self.over_rgba = self.over_rgba
            
    # handle norm later.. I'm thinking this can be accomplished with a
    # sort of transfer function from integer indices to indices

vtk_ax_order = [2,1,0]

# XXX: change to quick_convert_rgba_to_vtk_order
def quick_convert_rgba_to_vtk(arr):
    new_shape = arr.shape[:3][::-1] + (4,)
    arr_t = np.ravel(arr.transpose(), order='F').reshape(new_shape)
    return arr_t

def quick_convert_rgba_to_vtk_array(arr):
    new_shape = (np.prod(arr.shape[:3]), 4)
    arr_t = np.ravel(arr.transpose(), order='F').reshape(new_shape)
    return arr_t

class BlendedImages(BlendedArrays, ResampledIndexVolumeSlicer):
    """
    This class is a BlendedArrays object, whose main and over arrays
    are the index images from two ResampledIndexVolumeSlicers.

    It is also meant to expose a certain Mayavi ArraySource-like
    interface. Might think about making it a proper subclass.

    Notably...
    * It (possibly) enforces a strict array-to-spatial correspondence,
      to conform with the VTK ImageData layout
    * It provides image spacing/origin information, a la VTK ImageData
    * The RGBA arrays are intended to plug in nicely as 4 component
      point_data arrays

    """

    # the possibly mapped/scalar images
    main = t_ui.Any(comparison_mode=t_ui.NO_COMPARE)
    over = t_ui.Any(comparison_mode=t_ui.NO_COMPARE)

    main_spline_order = t_ui.Range(low=0,high=5,value=0)
    over_spline_order = t_ui.Range(low=0,high=5,value=0)
    vtk_order = t_ui.Bool(True)

    # Image properties of the blended image array
    img_spacing = t_ui.Property(depends_on='main')
    img_origin = t_ui.Property(depends_on='main')

    # Adapting to ResampledIndexVolumeSlicer spec
    image_arr = t_ui.Property #(depends_on='main_rgba, over_rgba')

    def __init__(self, **traits):
        # trap main and over traits here, since their setup depends
        # on other traits
        main = traits.pop('main', None)
        over = traits.pop('over', None)
        BlendedArrays.__init__(self, **traits)
        self.main = main
        self.over = over
        self._adapt_to_slicer()

    # XYZ: THIS HAS GOTTEN WAY TOO HACKY.. MUST FIX!
    def _adapt_to_slicer(self):
        bad_idx = cm.MixedAlphaColormap.i_bad
        # copy some attrs to match the blended image
        copied_attrs = ['bbox', '_ax_lookup', 'grid_spacing', 'coordmap']
        copied_from = self._prevailing_image()
        if copied_from:
            for attr in copied_attrs:
                setattr(self, attr, getattr(copied_from, attr))
            shape = self.image_arr.shape
            self.null_planes = [np.zeros((shape[0], shape[1], 4),'B'),
                                np.zeros((shape[0], shape[2], 4),'B'),
                                np.zeros((shape[1], shape[2], 4),'B')]
            
##             if self.vtk_order:
##                 cmap = self.coordmap
##                 self.coordmap = cmap.reordered_domain(
##                     cmap.function_domain.coord_names[::-1]
##                     )
##                 self._ax_lookup = vu.find_spatial_correspondence(self.coordmap)
            
            return
        # just fake numbers???
        self.bbox = [ (-10.,10.) ] * 3
        self.grid_spacing = np.array([1.]*3)
        self.coordmap = ni_api.AffineTransform.from_start_step(
            'ijk', xipy_ras, np.array([-10]*3), np.ones(3)
            )
        # THIS IS TRULY AWFUL!
        self._ax_lookup = dict( zip(range(3), range(3)) )
        o_coords = self.coordmap.function_range.coord_names
        self._ax_lookup.update( dict( zip(o_coords, range(3)) ) )
        self._ax_lookup.update( dict( zip(['SAG', 'COR', 'AXI'],
                                          range(3)) ) )
        shape = (10,10,10)
        self.null_planes = [np.zeros((shape[0], shape[1], 4),'B'),
                            np.zeros((shape[0], shape[2], 4),'B'),
                            np.zeros((shape[1], shape[2], 4),'B')]

            
    def _prevailing_image(self):
        if self.main:
            return self.main
        elif self.over:
            return self.over
        return None

    @t_ui.cached_property
    def _get_img_spacing(self):
        image = self._prevailing_image()
        if image is None:
            return None
        spacing = image.grid_spacing
        return spacing
    @t_ui.cached_property
    def _get_img_origin(self):
        # not sure what to do here
        image = self._prevailing_image()
        if image is None:
            return None
        origin = np.array(image.bbox)[:,0]
        return origin #[::-1] if self.vtk_order else origin

    def _get_image_arr(self):
        if not len(self.blended_rgba):
            return self.blended_rgba.reshape(0,0,0,4)
        else:
            return self.blended_rgba

    @t_ui.on_trait_change('main')
    def _update_mbytes(self):
        """Makes a ResampledIndexVolumeSlicer out of the argument, and
        enforces the spatial-to-array correspondence set up in the
        transpose mode.
        """
        if self.main==None:
            # "unload" main image
            self._main_idx = np.array([], np.int32)
            # trigger remapping of over index
            self.over = self.over
            return
        if isinstance(self.main, ni_api.Image):
            img = self.main
            spatial_axes = vtk_ax_order if self.vtk_order else None
            main = ResampledIndexVolumeSlicer(
                img, norm=self.main_norm,
                spatial_axes=spatial_axes,
                order=self.main_spline_order
                )
            # go ahead and be re-entrant
            self.main = main
            return
        if not isinstance(self.main, ResampledIndexVolumeSlicer):
            raise ValueError('main image should be a NIPY Image, or '\
                             'a ResampledIndexVolumeSlicer')
        # self.main is definitely the right type, but is it aligned?
        if self.vtk_order:
            # with VTK order, x,y,z must be aligned with k, j, i
            axes = vu.find_spatial_correspondence(self.main.coordmap)
            if axes != vtk_ax_order:
                # re-make a NIPY Image, and send it back!
                ni_image = ni_api.Image(
                    self.main.image_arr, self.main.coordmap
                    )
                self.main = ni_image
                return
            

        if len(self._over_idx) and \
               self.main.image_arr.shape != self._over_idx.shape:
            # in case anything is watching main_rgba, (which will change with
            # this update), temporarily set over_idx to an empty array so that
            # the blending is valid
            over_img = self.over
            self.over = None
            self._main_idx = self.main.image_arr
            # send the over image back through its vetting process
            self.over = over_img
        else:
            self._main_idx = self.main.image_arr
            # remap the over image (??)
            self.over = self.over
        self._adapt_to_slicer()

    @t_ui.on_trait_change('over')
    def _udpate_obytes(self):
        if self.over==None:
            self._over_idx = np.array([], np.int32)
            self._adapt_to_slicer()
            return
        # define the axes order to resample onto
        if self.vtk_order:
            ax_order = vtk_ax_order
        elif self.main:
            ax_order = vu.find_spatial_correspondence(self.main.coordmap)
        else:
            ax_order = None
        if type(self.over)==ni_api.Image:
            over = ResampledIndexVolumeSlicer(
                self.over, norm=self.over_norm,
                spatial_axes=ax_order,
                order=self.over_spline_order
                )
            # go ahead and be re-entrant
            self.over = over
            return
        if type(self.over) != ResampledIndexVolumeSlicer:
            raise ValueError('over image should be a NIPY Image, or '\
                             'a ResampledIndexVolumeSlicer')
        
        # self.over is definitely the right type, but is it aligned?
        if ax_order:
            axes = vu.find_spatial_correspondence(self.over.coordmap)
            if axes != ax_order:
                print 'Re-mapping because original mapping is not aligned '\
                'to the main image'
                # re-make a NIPY Image, and send it back!
                ni_image = ni_api.Image(
                    self.over.image_arr, self.over.coordmap
                    )
                self.over = ResampledIndexVolumeSlicer(
                    ni_image, norm=False, spatial_axes=ax_order,
                    order=self.over_spline_order
                    )
                return
            
        temp_idx = self.over.image_arr

        if not self.main:
            self._over_idx = temp_idx
            self._adapt_to_slicer()
        else:
            self.trait_setq(_over_idx=temp_idx)
            self._resample_over_into_main()

    def _resample_over_into_main(self):
        i_bad = self.over_cmap.i_bad
        vox_to_vox = ni_api.compose(
            self.over.coordmap.inverse(), self.main.coordmap
            )
        print vox_to_vox.affine
        print self._main_idx.shape
        print self._over_idx.shape
        # this is supposed to be diagonal!!
        mat = vox_to_vox.affine.diagonal()[:3]
        offset = vox_to_vox.affine[:3,-1]
##         self._over_idx = ndimage.affine_transform(
##             self._over_idx, mat, offset,
##             output_shape=self._main_idx.shape,
##             output=self._over_idx.dtype,
##             order=0, cval=i_bad)
        self._over_idx = resize_lookup_array(
            self._main_idx.shape, i_bad, self._over_idx, mat, offset
            )
                                             

