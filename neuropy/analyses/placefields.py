import io
from copy import deepcopy
from typing import Callable, Dict, Optional
from attrs import define, fields, filters, asdict, astuple
import h5py
import time
import matplotlib.pyplot as plt
from typing import Dict, List, Tuple, Optional, Callable, Union, Any
from typing_extensions import TypeAlias
from nptyping import NDArray
import neuropy.utils.type_aliases as types
import numpy as np
from matplotlib.gridspec import GridSpec
import pandas as pd
from scipy.ndimage import gaussian_filter, gaussian_filter1d, interpolation
from neuropy.core.epoch import Epoch, ensure_dataframe
from neuropy import core
from neuropy.core.position import Position
from neuropy.core.ratemap import Ratemap
from neuropy.core.flattened_spiketrains import SpikesAccessor # allows placefields to be sliced by neuron ids
from neuropy.utils.mixins.AttrsClassHelpers import AttrsBasedClassHelperMixin, serialized_field, serialized_attribute_field, non_serialized_field
from neuropy.utils.mixins.HDF5_representable import HDF_DeserializationMixin, post_deserialize, HDF_SerializationMixin, HDFMixin

from neuropy.plotting.figure import pretty_plot
from neuropy.plotting.mixins.placemap_mixins import PfnDPlottingMixin
from neuropy.utils.misc import is_iterable
from neuropy.utils.mixins.binning_helpers import BinnedPositionsMixin, bin_pos_nD, build_df_discretized_binned_position_columns

from neuropy.utils.mathutil import compute_grid_bin_bounds
from neuropy.utils.mixins.diffable import DiffableObject # for compute_placefields_as_needed type-hinting
from neuropy.utils.mixins.dict_representable import SubsettableDictRepresentable, DictInitializable, DictlikeOverridableMixin

from neuropy.utils.debug_helpers import safely_accepts_kwargs

from neuropy.utils.mixins.time_slicing import add_epochs_id_identity # for building direction pf's positions
from neuropy.utils.mixins.unit_slicing import NeuronUnitSlicableObjectProtocol
from neuropy.utils.mixins.peak_location_representing import PeakLocationRepresentingMixin, ContinuousPeakLocationRepresentingMixin
from neuropy.utils.mixins.gettable_mixin import KeypathsAccessibleMixin

# from .. import core
# import neuropy.core as core
from .. import plotting
from neuropy.utils.mixins.print_helpers import SimplePrintable, OrderedMeta, build_formatted_str_from_properties_dict

# ==================================================================================================================== #
# Formatting Override Globals                                                                                          #
# ==================================================================================================================== #
def _try_grid_bin_bounds_tuple_tuple_print_formatted(value) -> str:
	assert isinstance(value, (list, tuple)), f"type(value): {type(value)}"
	assert len(value) == 2, f"len(value): {len(value)} != 2"
	assert isinstance(value[0], (list, tuple)), f"type(value[0]): {type(value[0])}"
	assert len(value[0]) == 2, f"len(value[0]): {len(value[0])} != 2"
	assert isinstance(value[1], (list, tuple)), f"type(value[1]): {type(value[1])}"
	assert len(value[1]) == 2, f"len(value[1]): {len(value[1])} != 2"
	return f"(({value[0][0]:.3f}, {value[0][1]:.3f}), ({value[1][0]:.3f}, {value[1][1]:.3f}))"

custom_formatting_dict: Dict[str, Callable] = {'grid_bin_bounds': lambda value: _try_grid_bin_bounds_tuple_tuple_print_formatted(value)
}

custom_skip_formatting_display_list: List[str] = ['grid_bin_bounds', 'is_directional'] # items in custom_skip_formatting_display_list will not be displayed


class PlacefieldComputationParameters(SimplePrintable, KeypathsAccessibleMixin, SubsettableDictRepresentable, DictlikeOverridableMixin, DictInitializable, DiffableObject, metaclass=OrderedMeta):
	"""A simple wrapper object for parameters used in placefield calcuations
	
	#TODO 2023-07-30 18:18: - [ ] HDFMixin conformance for PlacefieldComputationParameters
	
	.grid_bin_bounds - specifies the outer extents (bounds) of the position grid in each spatial dimension (x & y)
	.grid_bin - specifies the fixed size of binning in each spatial dimension (x & y)
	
	
	"""
	decimal_point_character=","
	param_sep_char='-'
	variable_names=['speed_thresh', 'grid_bin', 'smooth', 'frate_thresh']
	variable_inline_names=['speedThresh', 'gridBin', 'smooth', 'frateThresh']
	variable_inline_names=['speedThresh', 'gridBin', 'smooth', 'frateThresh']
	# Note that I think it's okay to exclude `self.grid_bin_bounds` from these lists
	# print precision options:
	float_precision:int = 3
	array_items_threshold:int = 5
	


	def __init__(self, speed_thresh=3, grid_bin=2, grid_bin_bounds=None, smooth=2, frate_thresh=1, is_directional=False, **kwargs):
		self.speed_thresh = speed_thresh
		if not isinstance(grid_bin, (tuple, list)):
			grid_bin = (grid_bin, grid_bin) # make it into a 2 element tuple
		self.grid_bin = grid_bin
		if not isinstance(grid_bin_bounds, (tuple, list)):
			grid_bin_bounds = (grid_bin_bounds, grid_bin_bounds) # make it into a 2 element tuple
		self.grid_bin_bounds = grid_bin_bounds
		if not isinstance(smooth, (tuple, list)):
			smooth = (smooth, smooth) # make it into a 2 element tuple
		self.smooth = smooth
		self.frate_thresh = frate_thresh
		self.is_directional = is_directional

		# Dump all arguments into parameters.
		for key, value in kwargs.items():
			setattr(self, key, value)


	@property
	def grid_bin_1D(self):
		"""The grid_bin_1D property."""
		if np.isscalar(self.grid_bin):
			return self.grid_bin
		else:
			return self.grid_bin[0]

	@property
	def grid_bin_bounds_1D(self):
		"""The grid_bin_bounds property.
		It seems like even in 1D it's supposed to be returning a tuple (xmin, xmax), and not a float. 	
			
		"""
		if np.isscalar(self.grid_bin_bounds):
			return self.grid_bin_bounds
		else:
			return self.grid_bin_bounds[0]

	@property
	def smooth_1D(self):
		"""The smooth_1D property."""
		if np.isscalar(self.smooth):
			return self.smooth
		else:
			return self.smooth[0]

	def _unlisted_parameter_strings(self) -> List[str]:
		""" returns the string representations of all key/value pairs that aren't normally defined.
		NOTE: this seems generally useful!
		
		Uses: custom_formatting_dict
		"""
		# Dump all arguments into parameters.
		out_list = []
		for key, value in self.__dict__.items():
			if (key is not None) and (key not in PlacefieldComputationParameters.variable_names) and (key not in custom_skip_formatting_display_list):
				if value is None:
					out_list.append(f"{key}_None")
				else:
					# non-None
					if hasattr(value, 'str_for_filename'):
						out_list.append(f'{key}_{value.str_for_filename()}')
					elif hasattr(value, 'str_for_concise_display'):
						out_list.append(f'{key}_{value.str_for_concise_display()}')
					else:
						a_custom_formatting_fn = custom_formatting_dict.get(key, None)
						if a_custom_formatting_fn is not None:
							# have a valid custom formatting fcn
							_value_out_str: str = a_custom_formatting_fn(value)
							out_list.append(f"{key}_{_value_out_str}") ## default
						else:
							# no special handling methods:
							if isinstance(value, float):
								out_list.append(f"{key}_{value:.2f}")
							elif isinstance(value, np.ndarray):
								out_list.append(f'{key}_ndarray[{np.shape(value)}]')
							else:
								## try converting to NDArray
								was_conversion_success: bool = False
								try:
									_test_converted_value = np.array(value)
									with io.StringIO() as buf, np.printoptions(precision=self.float_precision, suppress=True, threshold=self.array_items_threshold):
										print(f"{_test_converted_value}", file=buf)
										_value_out_str: str = buf.getvalue()
										out_list.append(f"{key}_{_value_out_str}") ## default
									was_conversion_success = True
								except BaseException as e:
									print(f'conversion to NDArray failed for key "{key}", value: {value} with error {e}')
									was_conversion_success = False
									pass
								
								# No special handling:
								if not was_conversion_success:
									try:
										out_list.append(f"{key}_{value}") ## default
									except BaseException as e:
										print(f'UNEXPECTED_EXCEPTION: {e}')
										print(f'self.__dict__: {self.__dict__}')
										raise e

		return out_list


	def str_for_filename(self, is_2D: bool):
		with np.printoptions(precision=self.float_precision, suppress=True, threshold=self.array_items_threshold):
			# score_text = f"score: " + str(np.array([epoch_score])).lstrip("[").rstrip("]") # output is just the number, as initially it is '[0.67]' but then the [ and ] are stripped.            
			extras_strings: List[str] = self._unlisted_parameter_strings()
			if is_2D:
				return '-'.join([f"speedThresh_{self.speed_thresh:.2f}", f"gridBin_{self.grid_bin[0]:.2f}_{self.grid_bin[1]:.2f}", f"smooth_{self.smooth[0]:.2f}_{self.smooth[1]:.2f}", f"frateThresh_{self.frate_thresh:.2f}", *extras_strings])
			else:
				return '-'.join([f"speedThresh_{self.speed_thresh:.2f}", f"gridBin_{self.grid_bin_1D:.2f}", f"smooth_{self.smooth_1D:.2f}", f"frateThresh_{self.frate_thresh:.2f}", *extras_strings])

	def str_for_display(self, is_2D: bool, extras_join_sep: str=', ', normal_to_extras_line_sep:str=''):
		""" For rendering in a title, etc
		normal_to_extras_line_sep: the separator between the normal and extras lines
		
		#TODO 2023-07-21 16:35: - [ ] The np.printoptions doesn't affect the values that are returned from `extras_string = ', '.join(self._unlisted_parameter_strings())`
		We end up with '(speedThresh_10.00, gridBin_2.00, smooth_2.00, frateThresh_1.00)grid_bin_bounds_((25.5637332724328, 257.964172947664), (89.1844223602494, 131.92462510535915))' (too many sig-figs on the output grid_bin_bounds)
		
		"""
		with np.printoptions(precision=self.float_precision, suppress=True, threshold=self.array_items_threshold):
			extras_string = extras_join_sep.join(self._unlisted_parameter_strings()) # ['grid_bin_bounds_((37.0773897438341, 250.69004399129707), (137.97626338793503, 146.00371440346137))', 'is_directional_False']
			if is_2D:
				return f"(speedThresh_{self.speed_thresh:.2f}, gridBin_{self.grid_bin[0]:.2f}_{self.grid_bin[1]:.2f}, smooth_{self.smooth[0]:.2f}_{self.smooth[1]:.2f}, frateThresh_{self.frate_thresh:.2f})" + normal_to_extras_line_sep + extras_string
			else:
				return f"(speedThresh_{self.speed_thresh:.2f}, gridBin_{self.grid_bin_1D:.2f}, smooth_{self.smooth_1D:.2f}, frateThresh_{self.frate_thresh:.2f})" + normal_to_extras_line_sep + extras_string


	def str_for_attributes_list_display(self, param_sep_char='\n', key_val_sep_char='\t', subset_includelist:Optional[list]=None, subset_excludelist:Optional[list]=None, override_float_precision:Optional[int]=None, override_array_items_threshold:Optional[int]=None):
		""" For rendering in attributes list like outputs
		# Default for attributes lists outputs:
		Example Output:
			speed_thresh	2.0
			grid_bin	[3.777 1.043]
			smooth	[1.5 1.5]
			frate_thresh	0.1
			time_bin_size	0.5
		"""
		return build_formatted_str_from_properties_dict(self.to_dict(subset_includelist=subset_includelist, subset_excludelist=subset_excludelist), param_sep_char, key_val_sep_char, float_precision=(override_float_precision or self.float_precision), array_items_threshold=(override_array_items_threshold or self.array_items_threshold))


	def __hash__(self):
		""" custom hash function that allows use in dictionary just based off of the values and not the object instance. """
		dict_rep = self.to_dict()
		member_names_tuple = list(dict_rep.keys())
		values_tuple = list(dict_rep.values())
		combined_tuple = tuple(member_names_tuple + values_tuple)
		return hash(combined_tuple)
	

	def __eq__(self, other):
		"""Overrides the default implementation to allow comparing by value. """
		if isinstance(other, PlacefieldComputationParameters):
			return self.to_dict() == other.to_dict() # Python's dicts use element-wise comparison by default, so this is what we want.
		else:
			raise NotImplementedError
		return NotImplemented # this part looks like a bug, yeah?

	
	@classmethod
	def compute_grid_bin_bounds(cls, x, y):
		return compute_grid_bin_bounds(x, y)



def _normalized_occupancy(raw_occupancy, position_srate=None):
	"""Computes seconds_occupancy and normalized_occupancy from the raw_occupancy. See Returns section for definitions and more info.

	Args:
		raw_occupancy (_type_): *raw occupancy* is defined in terms of the number of position samples that fall into each bin.
		position_srate (_type_, optional): Sampling rate in Hz (1/[sec])

	Returns:
		tuple<float,float>: (seconds_occupancy, normalized_occupancy)
			*seconds_occupancy* is the number of seconds spent in each bin. This is computed by multiplying the raw occupancy (in # samples) by the duration of each sample.
			**normalized occupancy** gives the ratio of samples that fall in each bin. ALL BINS ADD UP TO ONE.
	"""

	# if position_srate is not None:
	#     dt = 1.0 / float(position_srate)
	#
	# seconds_occupancy = raw_occupancy * dt  # converting to seconds
	seconds_occupancy = raw_occupancy / (float(position_srate) + 1e-16) # converting to seconds

	# + 1e-16 is added to prevent `FloatingPointError: invalid value encountered in divide` for 0/0
	normalized_occupancy = raw_occupancy / (np.nansum(raw_occupancy) + 1e-16) # the normalized occupancy determines the relative number of samples spent in each bin

	return seconds_occupancy, normalized_occupancy



class PfnConfigMixin:
	def str_for_filename(self, is_2D=True):
		return self.config.str_for_filename(is_2D)


class PfnDMixin(SimplePrintable):

	should_smooth_speed = False
	should_smooth_spikes_map = False
	should_smooth_spatial_occupancy_map = False
	should_smooth_final_tuning_map = True

	@property
	def spk_pos(self):
		return self.ratemap_spiketrains_pos

	@property
	def spk_t(self):
		return self.ratemap_spiketrains

	@property
	def cell_ids(self):
		return self.ratemap.neuron_ids

	@safely_accepts_kwargs
	def plot_raw(self, subplots=(10, 8), fignum=None, alpha=0.5, label_cells=False, ax=None, clus_use=None):
		""" Plots the Placefield raw spiking activity for all cells"""
		if self.ndim < 2:
			## TODO: Pf1D Temporary Workaround:
			return plotting.plot_raw(self.ratemap, self.t, self.x, 'BOTH', ax=ax, subplots=subplots)
		else:
			if ax is None:
				fig = plt.figure(fignum, figsize=(12, 20))
				gs = GridSpec(subplots[0], subplots[1], figure=fig)
				# fig.subplots_adjust(hspace=0.4)
			else:
				assert len(ax) == len(clus_use), "Number of axes must match number of clusters to plot"
				fig = ax[0].get_figure()

			# spk_pos_use = self.spk_pos
			spk_pos_use = self.ratemap_spiketrains_pos

			if clus_use is not None:
				spk_pos_tmp = spk_pos_use
				spk_pos_use = []
				[spk_pos_use.append(spk_pos_tmp[a]) for a in clus_use]

			for cell, (spk_x, spk_y) in enumerate(spk_pos_use):
				if ax is None:
					ax1 = fig.add_subplot(gs[cell])
				else:
					ax1 = ax[cell]
				ax1.plot(self.x, self.y, color="#d3c5c5") # Plot the animal's position. This will be the same for all cells
				ax1.plot(spk_x, spk_y, '.', markersize=0.8, color=[1, 0, 0, alpha]) # plot the cell-specific spike locations
				ax1.axis("off")
				if label_cells:
					# Put cell info (id, etc) on title
					info = self.cell_ids[cell]
					ax1.set_title(f"Cell {info}")

			fig.suptitle(f"Place maps for cells with their peak firing rate (frate thresh={self.frate_thresh},speed_thresh={self.speed_thresh})")
			return fig

	@safely_accepts_kwargs
	def plotRaw_v_time(self, cellind, speed_thresh=False, spikes_color=None, spikes_alpha=None, ax=None, position_plot_kwargs=None, spike_plot_kwargs=None,
		should_include_trajectory=True, should_include_spikes=True, should_include_filter_excluded_spikes=True, should_include_labels=True, use_filtered_positions=False, use_pandas_plotting=False):
		""" Builds one subplot for each dimension of the position data
		Updated to work with both 1D and 2D Placefields

		should_include_trajectory:bool - if False, will not try to plot the animal's trajectory/position
			NOTE: Draws the spike_positions actually instead of the continuously sampled animal position

		should_include_labels:bool - whether the plot should include text labels, like the title, axes labels, etc
		should_include_spikes:bool - if False, will not try to plot points for spikes
		use_pandas_plotting:bool = False
		use_filtered_positions:bool = False # If True, uses only the filtered positions (which are missing the end caps) and the default a.plot(...) results in connected lines which look bad.

		"""
		if ax is None:
			fig, ax = plt.subplots(self.ndim, 1, sharex=True)
			fig.set_size_inches([23, 9.7])

		if not is_iterable(ax):
			ax = [ax]

		# plot trajectories
		pos_df = self.position.to_dataframe()
		
		# self.x, self.y contain filtered positions, pos_df's columns contain all positions.
		if not use_pandas_plotting: # don't need to worry about 't' for pandas plotting, we'll just use the one in the dataframe.
			if use_filtered_positions:
				t = self.t
			else:
				t = pos_df.t.to_numpy()

		if self.ndim < 2:
			if not use_pandas_plotting:
				if use_filtered_positions:
					variable_array = [self.x]
				else:
					variable_array = [pos_df.x.to_numpy()]
			else:
				variable_array = ['x']
			label_array = ["X position (cm)"]
		else:
			if not use_pandas_plotting:
				if use_filtered_positions:
					variable_array = [self.x, self.y]
				else:
					variable_array = [pos_df.x.to_numpy(), pos_df.y.to_numpy()]
			else:
				variable_array = ['x', 'y']
			label_array = ["X position (cm)", "Y position (cm)"]

		for a, pos, ylabel in zip(ax, variable_array, label_array):
			if should_include_trajectory:
				if not use_pandas_plotting:
					a.plot(t, pos, **(position_plot_kwargs or {}))
				else:
					pos_df.plot(x='t', y=pos, ax=a, legend=False, **(position_plot_kwargs or {})) # changed to pandas.plot because the filtered positions were missing the end caps, and the default a.plot(...) resulted in connected lines which looked bad.

			if should_include_labels:
				a.set_xlabel("Time (seconds)")
				a.set_ylabel(ylabel)
			pretty_plot(a)

		# plot spikes on trajectory
		if cellind is not None:
			if should_include_spikes:
				# Grab correct spike times/positions
				if speed_thresh and (not should_include_filter_excluded_spikes):
					spk_pos_, spk_t_ = self.run_spk_pos, self.run_spk_t # TODO: these don't exist
				else:
					spk_pos_, spk_t_ = self.spk_pos, self.spk_t

				if spike_plot_kwargs is None:
					spike_plot_kwargs = {}

				if spikes_alpha is None:
					spikes_alpha = 0.5 # default value of 0.5

				if spikes_color is not None:
					spikes_color_RGBA = [*spikes_color, spikes_alpha]
					# Check for existing values in spike_plot_kwargs which will be overriden
					markerfacecolor = spike_plot_kwargs.get('markerfacecolor', None)
					# markeredgecolor = spike_plot_kwargs.get('markeredgecolor', None)
					if markerfacecolor is not None:
						if markerfacecolor != spikes_color_RGBA:
							print(f"WARNING: spike_plot_kwargs's extant 'markerfacecolor' and 'markeredgecolor' values will be overriden by the provided spikes_color argument, meaning its original value will be lost.")
							spike_plot_kwargs['markerfacecolor'] = spikes_color_RGBA
							spike_plot_kwargs['markeredgecolor'] = spikes_color_RGBA
				else:
					# assign the default
					spikes_color_RGBA = [*(0, 0, 0.8), spikes_alpha]

				for a, pos in zip(ax, spk_pos_[cellind]):
					# WARNING: if spike_plot_kwargs contains the 'markerfacecolor' key, it's value will override plot's color= argument, so the specified spikes_color will be ignored.
					a.plot(spk_t_[cellind], pos, color=spikes_color_RGBA, **(spike_plot_kwargs or {})) # , color=[*spikes_color, spikes_alpha]
					#TODO 2023-09-06 02:23: - [ ] Note that without extra `spike_plot_kwargs` this plots spikes as connected lines without markers which is nearly always wrong.

			# Put info on title
			if should_include_labels:
				ax[0].set_title(
					"Cell "
					+ str(self.cell_ids[cellind])
					+ ":, speed_thresh="
					+ str(self.speed_thresh)
				)
		return ax

	@safely_accepts_kwargs
	def plot_all(self, cellind, speed_thresh=True, spikes_color=(0, 0, 0.8), spikes_alpha=0.4, fig=None):
		if fig is None:
			fig_use = plt.figure(figsize=[28.25, 11.75])
		else:
			fig_use = fig
		gs = GridSpec(2, 4, figure=fig_use)
		ax2d = fig_use.add_subplot(gs[0, 0])
		axccg = np.asarray(fig_use.add_subplot(gs[1, 0]))
		axx = fig_use.add_subplot(gs[0, 1:])
		axy = fig_use.add_subplot(gs[1, 1:], sharex=axx)

		self.plot_raw(speed_thresh=speed_thresh, clus_use=[cellind], ax=[ax2d])
		self.plotRaw_v_time(cellind, speed_thresh=speed_thresh, ax=[axx, axy], spikes_color=spikes_color, spikes_alpha=spikes_alpha)
		self._obj.spikes.plot_ccg(clus_use=[cellind], type="acg", ax=axccg)

		return fig_use


class Pf1D(PfnConfigMixin, PfnDMixin):

	@staticmethod
	def _compute_occupancy(x, xbin, position_srate, smooth, should_return_num_pos_samples_occupancy=False):
		"""  occupancy map calculations

		should_return_num_pos_samples_occupancy:bool - If True, the occupanies returned are specified in number of pos samples. Otherwise, they're returned in units of seconds.
		"""
		# --- occupancy map calculation -----------
		# NRK todo: might need to normalize occupancy so sum adds up to 1
		num_pos_samples_unsmoothed_occupancy, xedges = np.histogram(x, bins=xbin)
		if ((smooth is not None) and (smooth > 0.0)):
			num_pos_samples_occupancy = gaussian_filter1d(num_pos_samples_unsmoothed_occupancy, sigma=smooth)
		else:
			num_pos_samples_occupancy = num_pos_samples_unsmoothed_occupancy
		# # raw occupancy is defined in terms of the number of samples that fall into each bin.

		if should_return_num_pos_samples_occupancy:
			return num_pos_samples_occupancy, num_pos_samples_unsmoothed_occupancy, xedges
		else:
			seconds_unsmoothed_occupancy, normalized_unsmoothed_occupancy = _normalized_occupancy(num_pos_samples_unsmoothed_occupancy, position_srate=position_srate)
			seconds_occupancy, normalized_occupancy = _normalized_occupancy(num_pos_samples_occupancy, position_srate=position_srate)
			return seconds_occupancy, seconds_unsmoothed_occupancy, xedges

	@staticmethod
	def _compute_spikes_map(spk_x, xbin, smooth):
		unsmoothed_spikes_map = np.histogram(spk_x, bins=xbin)[0]
		if ((smooth is not None) and (smooth > 0.0)):
			spikes_map = gaussian_filter1d(unsmoothed_spikes_map, sigma=smooth)
		else:
			spikes_map = unsmoothed_spikes_map
		return spikes_map, unsmoothed_spikes_map

	@staticmethod
	def _compute_tuning_map(spk_x, xbin, occupancy, smooth, should_also_return_intermediate_spikes_map=False):
		if not PfnDMixin.should_smooth_spikes_map:
			smooth_spikes_map = None
		else:
			smooth_spikes_map = smooth
		spikes_map, unsmoothed_spikes_map = Pf1D._compute_spikes_map(spk_x, xbin, smooth_spikes_map)

		## Copied from Pf2D._compute_tuning_map to handle zero occupancy locations:
		occupancy[occupancy == 0.0] = np.nan # pre-set the zero occupancy locations to NaN to avoid a warning in the next step. They'll be replaced with zero afterwards anyway
		never_smoothed_occupancy_weighted_tuning_map = unsmoothed_spikes_map / occupancy # dividing by positions with zero occupancy result in a warning and the result being set to NaN. Set to 0.0 instead.
		never_smoothed_occupancy_weighted_tuning_map = np.nan_to_num(never_smoothed_occupancy_weighted_tuning_map, copy=True, nan=0.0) # set any NaN values to 0.0, as this is the correct weighted occupancy
		unsmoothed_occupancy_weighted_tuning_map = spikes_map / occupancy # dividing by positions with zero occupancy result in a warning and the result being set to NaN. Set to 0.0 instead.
		unsmoothed_occupancy_weighted_tuning_map = np.nan_to_num(unsmoothed_occupancy_weighted_tuning_map, copy=True, nan=0.0) # set any NaN values to 0.0, as this is the correct weighted occupancy
		occupancy[np.isnan(occupancy)] = 0.0 # restore these entries back to zero

		if PfnDMixin.should_smooth_final_tuning_map and ((smooth is not None) and (smooth > 0.0)):
			occupancy_weighted_tuning_map = gaussian_filter1d(unsmoothed_occupancy_weighted_tuning_map, sigma=smooth)
		else:
			occupancy_weighted_tuning_map = unsmoothed_occupancy_weighted_tuning_map

		if should_also_return_intermediate_spikes_map:
			return occupancy_weighted_tuning_map, never_smoothed_occupancy_weighted_tuning_map, spikes_map, unsmoothed_spikes_map
		else:
			return occupancy_weighted_tuning_map, never_smoothed_occupancy_weighted_tuning_map




class Pf2D(PfnConfigMixin, PfnDMixin):

	@staticmethod
	def _compute_occupancy(x, y, xbin, ybin, position_srate, smooth, should_return_num_pos_samples_occupancy=False):
		"""  occupancy map calculations

		should_return_num_pos_samples_occupancy:bool - If True, the occupanies returned are specified in number of pos samples. Otherwise, they're returned in units of seconds.

		"""
		# --------------
		# NRK todo: might need to normalize occupancy so sum adds up to 1
		# Please note that the histogram does not follow the Cartesian convention where x values are on the abscissa and y values on the ordinate axis. Rather, x is histogrammed along the first dimension of the array (vertical), and y along the second dimension of the array (horizontal).
		num_pos_samples_unsmoothed_occupancy, xedges, yedges = np.histogram2d(x, y, bins=(xbin, ybin))
		# occupancy = occupancy.T # transpose the occupancy before applying other operations
		# raw_occupancy = raw_occupancy / position_srate + 10e-16  # converting to seconds
		if ((smooth is not None) and ((smooth[0] > 0.0) & (smooth[1] > 0.0))):
			num_pos_samples_occupancy = gaussian_filter(num_pos_samples_unsmoothed_occupancy, sigma=(smooth[1], smooth[0])) # 2d gaussian filter: need to flip smooth because the x and y are transposed
		else:
			num_pos_samples_occupancy = num_pos_samples_unsmoothed_occupancy
		# Histogram does not follow Cartesian convention (see Notes),
		# therefore transpose occupancy for visualization purposes.
		# raw occupancy is defined in terms of the number of samples that fall into each bin.
		if should_return_num_pos_samples_occupancy:
			return num_pos_samples_occupancy, num_pos_samples_unsmoothed_occupancy, xedges, yedges
		else:
			seconds_unsmoothed_occupancy, normalized_unsmoothed_occupancy = _normalized_occupancy(num_pos_samples_unsmoothed_occupancy, position_srate=position_srate)
			seconds_occupancy, normalized_occupancy = _normalized_occupancy(num_pos_samples_occupancy, position_srate=position_srate)
			return seconds_occupancy, seconds_unsmoothed_occupancy, xedges, yedges

	@staticmethod
	def _compute_spikes_map(spk_x, spk_y, xbin, ybin, smooth):
		# spikes_map: is the number of spike counts in each bin for this unit
		unsmoothed_spikes_map = np.histogram2d(spk_x, spk_y, bins=(xbin, ybin))[0]
		if ((smooth is not None) and ((smooth[0] > 0.0) & (smooth[1] > 0.0))):
			spikes_map = gaussian_filter(unsmoothed_spikes_map, sigma=(smooth[1], smooth[0])) # 2d gaussian filter: need to flip smooth because the x and y are transposed
		else:
			spikes_map = unsmoothed_spikes_map
		return spikes_map, unsmoothed_spikes_map

	@staticmethod
	def _compute_tuning_map(spk_x, spk_y, xbin, ybin, occupancy, smooth, should_also_return_intermediate_spikes_map=False):
		# raw_tuning_map: is the number of spike counts in each bin for this unit
		if not PfnDMixin.should_smooth_spikes_map:
			smoothing_widths_spikes_map = None
		else:
			smoothing_widths_spikes_map = smooth
		spikes_map, unsmoothed_spikes_map = Pf2D._compute_spikes_map(spk_x, spk_y, xbin, ybin, smoothing_widths_spikes_map)

		occupancy[occupancy == 0.0] = np.nan # pre-set the zero occupancy locations to NaN to avoid a warning in the next step. They'll be replaced with zero afterwards anyway
		never_smoothed_occupancy_weighted_tuning_map = unsmoothed_spikes_map / occupancy # dividing by positions with zero occupancy result in a warning and the result being set to NaN. Set to 0.0 instead.
		never_smoothed_occupancy_weighted_tuning_map = np.nan_to_num(never_smoothed_occupancy_weighted_tuning_map, copy=True, nan=0.0) # set any NaN values to 0.0, as this is the correct weighted occupancy
		unsmoothed_occupancy_weighted_tuning_map = spikes_map / occupancy # dividing by positions with zero occupancy result in a warning and the result being set to NaN. Set to 0.0 instead.
		unsmoothed_occupancy_weighted_tuning_map = np.nan_to_num(unsmoothed_occupancy_weighted_tuning_map, copy=True, nan=0.0) # set any NaN values to 0.0, as this is the correct weighted occupancy
		occupancy[np.isnan(occupancy)] = 0.0 # restore these entries back to zero

		if PfnDMixin.should_smooth_final_tuning_map and ((smooth is not None) and ((smooth[0] > 0.0) & (smooth[1] > 0.0))):
			occupancy_weighted_tuning_map = gaussian_filter(unsmoothed_occupancy_weighted_tuning_map, sigma=(smooth[1], smooth[0])) # need to flip smooth because the x and y are transposed
		else:
			occupancy_weighted_tuning_map = unsmoothed_occupancy_weighted_tuning_map

		if should_also_return_intermediate_spikes_map:
			return occupancy_weighted_tuning_map, never_smoothed_occupancy_weighted_tuning_map, spikes_map, unsmoothed_spikes_map
		else:
			return occupancy_weighted_tuning_map, never_smoothed_occupancy_weighted_tuning_map




class PlacefieldND(PfnConfigMixin, PfnDMixin):
	""" 2023-11-10 ChatGPT-3 Generalized Implementation, UNTESTED
	# 1D:
	def _compute_occupancy(x, xbin, position_srate, smooth, should_return_num_pos_samples_occupancy=False)
	def _compute_spikes_map(spk_x, xbin, smooth)
	def _compute_tuning_map(spk_x, xbin, occupancy, smooth, should_also_return_intermediate_spikes_map=False)

	# 2D:
	def _compute_occupancy(x, y, xbin, ybin, position_srate, smooth, should_return_num_pos_samples_occupancy=False):
	def _compute_spikes_map(spk_x, spk_y, xbin, ybin, smooth)
	def _compute_tuning_map(spk_x, spk_y, xbin, ybin, occupancy, smooth, should_also_return_intermediate_spikes_map=False)

	# 3D:
	position_args: x, y, z
	bin_args: xbin, ybin, zbin
	spike_args: spk_x, spk_y, spk_z
	def _compute_occupancy(x, y, z, xbin, ybin, zbin, position_srate, smooth, should_return_num_pos_samples_occupancy=False):
	def _compute_spikes_map(spk_x, spk_y, spk_z, xbin, ybin, zbin, smooth)
	def _compute_tuning_map(spk_x, spk_y, spk_z, xbin, ybin, zbin, occupancy, smooth, should_also_return_intermediate_spikes_map=False)


	# N-D
	position_args: x, y, z
	bin_args: xbin, ybin, zbin
	spike_args: spk_x, spk_y, spk_z
	def _compute_occupancy(x, y, z, xbin, ybin, zbin, position_srate, smooth, should_return_num_pos_samples_occupancy=False):
	def _compute_spikes_map(spk_x, spk_y, spk_z, xbin, ybin, zbin, smooth)
	def _compute_tuning_map(spk_x, spk_y, spk_z, xbin, ybin, zbin, occupancy, smooth, should_also_return_intermediate_spikes_map=False)


	from neuropy.analyses.placefields import Pf1D, Pf2D, PlacefieldND


	"""


	@staticmethod
	def _compute_occupancy(position, bins, position_srate, smooth, should_return_num_pos_samples_occupancy=False):
		num_pos_samples_unsmoothed_occupancy, edges = np.histogramdd(position, bins=bins)
		
		if smooth is not None and any(s > 0.0 for s in smooth):
			num_pos_samples_occupancy = gaussian_filter(num_pos_samples_unsmoothed_occupancy, sigma=smooth)
		else:
			num_pos_samples_occupancy = num_pos_samples_unsmoothed_occupancy
		
		if should_return_num_pos_samples_occupancy:
			return num_pos_samples_occupancy, num_pos_samples_unsmoothed_occupancy, edges
		else:
			seconds_unsmoothed_occupancy, normalized_unsmoothed_occupancy = _normalized_occupancy(num_pos_samples_unsmoothed_occupancy, position_srate=position_srate)
			seconds_occupancy, normalized_occupancy = _normalized_occupancy(num_pos_samples_occupancy, position_srate=position_srate)
			return seconds_occupancy, seconds_unsmoothed_occupancy, edges

	@staticmethod
	def _compute_spikes_map(spikes, bins, smooth):
		unsmoothed_spikes_map = np.histogramdd(spikes, bins=bins)[0]
		
		if smooth is not None and any(s > 0.0 for s in smooth):
			spikes_map = gaussian_filter(unsmoothed_spikes_map, sigma=smooth)
		else:
			spikes_map = unsmoothed_spikes_map
		
		return spikes_map, unsmoothed_spikes_map

	@staticmethod
	def _compute_tuning_map(spikes, bins, occupancy, smooth, should_also_return_intermediate_spikes_map=False):
		if not PfnDMixin.should_smooth_spikes_map:
			smoothing_widths_spikes_map = None
		else:
			smoothing_widths_spikes_map = smooth
		
		spikes_map, unsmoothed_spikes_map = PlacefieldND._compute_spikes_map(spikes, bins, smoothing_widths_spikes_map)

		occupancy[occupancy == 0.0] = np.nan
		never_smoothed_occupancy_weighted_tuning_map = unsmoothed_spikes_map / occupancy
		never_smoothed_occupancy_weighted_tuning_map = np.nan_to_num(never_smoothed_occupancy_weighted_tuning_map, copy=True, nan=0.0)
		unsmoothed_occupancy_weighted_tuning_map = spikes_map / occupancy
		unsmoothed_occupancy_weighted_tuning_map = np.nan_to_num(unsmoothed_occupancy_weighted_tuning_map, copy=True, nan=0.0)
		occupancy[np.isnan(occupancy)] = 0.0

		if PfnDMixin.should_smooth_final_tuning_map and (smooth is not None and any(s > 0.0 for s in smooth)):
			occupancy_weighted_tuning_map = gaussian_filter(unsmoothed_occupancy_weighted_tuning_map, sigma=smooth)
		else:
			occupancy_weighted_tuning_map = unsmoothed_occupancy_weighted_tuning_map

		if should_also_return_intermediate_spikes_map:
			return occupancy_weighted_tuning_map, never_smoothed_occupancy_weighted_tuning_map, spikes_map, unsmoothed_spikes_map
		else:
			return occupancy_weighted_tuning_map, never_smoothed_occupancy_weighted_tuning_map





# First, interested in answering the question "where did the animal spend its time on the track" to assess the relative frequency of events that occur in a given region. If the animal spends a lot of time in a certain region,
# it's more likely that any cell, not just the ones that hold it as a valid place field, will fire there.
	# this can be done by either binning (lumping close position points together based on a standardized grid), neighborhooding, or continuous smearing.


		
@define(slots=False)
class PfND(HDFMixin, AttrsBasedClassHelperMixin, ContinuousPeakLocationRepresentingMixin, PeakLocationRepresentingMixin, NeuronUnitSlicableObjectProtocol, BinnedPositionsMixin, PfnConfigMixin, PfnDMixin, PfnDPlottingMixin):
	"""Represents a collection of placefields over binned,  N-dimensional space. 

		It always computes two place maps with and without speed thresholds.

		Parameters
		----------
		spikes_df: pd.DataFrame
		position : core.Position
		epochs : core.Epoch
			specifies the list of epochs to include.
		grid_bin : int
			bin size of position bining, by default 5
		speed_thresh : int
			speed threshold for calculating place field


		# Excluded from serialization: ['_included_thresh_neurons_indx', '_peak_frate_filter_function']
	"""
	spikes_df: pd.DataFrame # spikes_df shouldn't ever be updated
	position: Position
	epochs: Epoch = None
	config: PlacefieldComputationParameters = None
	position_srate: float = None
	
	setup_on_init: bool = True
	compute_on_init: bool = True
	_save_intermediate_spikes_maps: bool = True

	_included_thresh_neurons_indx: np.ndarray = None
	_peak_frate_filter_function: Callable = None

	_ratemap: Ratemap = None
	_ratemap_spiketrains: list = None
	_ratemap_spiketrains_pos: list = None

	_filtered_pos_df: pd.DataFrame = None
	_filtered_spikes_df: pd.DataFrame = None

	ndim: int = None
	xbin: np.ndarray = None
	ybin: np.ndarray = None
	bin_info: dict = None # dict with keys: ['mode', 'xstep', 'xnum_bins'] and if 2D ['ystep', 'ynum_bins']

	def __attrs_post_init__(self):
		""" called after initializer built by `attrs` library. """
		# Perform the primary setup to build the placefield
		if self.setup_on_init:
			self.setup(self.position, self.spikes_df, self.epochs)
			if self.compute_on_init:
				self.compute()
		else:
			assert (not self.compute_on_init), f"compute_on_init can't be true if setup_on_init isn't true!"

	@classmethod
	def from_config_values(cls, spikes_df: pd.DataFrame, position: Position, epochs: Epoch = None, frate_thresh=1, speed_thresh=5, grid_bin=(1,1), grid_bin_bounds=None, smooth=(1,1), setup_on_init:bool=True, compute_on_init:bool=True):
		""" initialize from the explicitly listed arguments instead of a specified config. """
		return cls(spikes_df=spikes_df, position=position, epochs=epochs,
			config=PlacefieldComputationParameters(speed_thresh=speed_thresh, grid_bin=grid_bin, grid_bin_bounds=grid_bin_bounds, smooth=smooth, frate_thresh=frate_thresh),
			setup_on_init=setup_on_init, compute_on_init=compute_on_init, position_srate=position.sampling_rate)


	def setup(self, position: Position, spikes_df, epochs: Epoch, debug_print=False):
		""" do the preliminary setup required to build the placefields

		Adds columns to the spikes and positions dataframes, etc.

		Depends on:
			self.config.smooth
			self.config.grid_bin_bounds

		Assigns:
			self.ndim
			self._filtered_pos_df
			self._filtered_spikes_df

			self.xbin, self.ybin, self.bin_info
		"""
		use_modern_speed_threshold_filtering_preserving_occupancy: bool = True

		# Set the dimensionality of the PfND object from the position's dimensionality
		self.ndim = position.ndim
		self.position_srate = position.sampling_rate

		pos_df = position.to_dataframe()
		spk_df = spikes_df.copy()

		# filtering:
		if epochs is not None:
			# filter the spikes_df:
			self._filtered_spikes_df = spk_df.spikes.time_sliced(epochs.starts, epochs.stops)
			# filter the pos_df:
			self._filtered_pos_df = pos_df.position.time_sliced(epochs.starts, epochs.stops) # 5378 rows × 18 columns
		else:
			# if no epochs filtering, set the filtered objects to be sliced by the available range of the position data (given by position.t_start, position.t_stop)
			self._filtered_spikes_df = spk_df.spikes.time_sliced(position.t_start, position.t_stop)
			self._filtered_pos_df = pos_df.position.time_sliced(position.t_start, position.t_stop)

		# drop positions with either X or Y NA values:

		if (self.ndim > 1):
			pos_non_NA_column_labels = ['x','y']
		else:
			pos_non_NA_column_labels = ['x']

		self._filtered_pos_df.dropna(axis=0, how='any', subset=pos_non_NA_column_labels, inplace=True) # dropped NaN values

		# Set animal observed position member variables:
		speed_column_name: str = 'speed'
		if ((self.should_smooth_speed and (self.config.smooth is not None) and (self.config.smooth[0] > 0.0))):
			self._filtered_pos_df['speed_smooth'] = gaussian_filter1d(self._filtered_pos_df.speed.to_numpy(), sigma=self.config.smooth[0])
			#TODO 2023-11-10 16:30: - [ ] This seems to only use the 1D speed even for 2D placefields, which could be an issue.
			speed_column_name: str = 'speed_smooth'

		# Filter for speed:
		if debug_print:
			print(f'pre speed filtering: {np.shape(self._filtered_spikes_df)[0]} spikes.')
		
		if use_modern_speed_threshold_filtering_preserving_occupancy:
			# 2023-11-14 - Modern Occupancy-safe speed filtering:
			epochs_df = deepcopy(epochs.to_dataframe())
			speed_filtered_epochs_df = PfND.filtered_by_speed(epochs_df, position_df=self._filtered_pos_df, speed_thresh=self.config.speed_thresh, speed_column_override_name=speed_column_name, debug_print=False)
			if speed_filtered_epochs_df is not None:
				# filter the spikes_df:
				self._filtered_spikes_df = spk_df.spikes.time_sliced(speed_filtered_epochs_df.epochs.starts, speed_filtered_epochs_df.epochs.stops)
				# filter the pos_df:
				self._filtered_pos_df = pos_df.position.time_sliced(speed_filtered_epochs_df.epochs.starts, speed_filtered_epochs_df.epochs.stops) # 5378 rows × 18 columns

			epochs = Epoch(speed_filtered_epochs_df)

			# Add interpolated velocity information to spikes dataframe (just for compatibility):
			if 'speed' not in self._filtered_spikes_df.columns:
				self._filtered_spikes_df['speed'] = np.interp(self._filtered_spikes_df[spikes_df.spikes.time_variable_name].to_numpy(), self.filtered_pos_df.t.to_numpy(), self.speed) ## NOTE: self.speed is either the regular ['speed'] column of the position_df OR the 'speed_smooth'] column if self.should_smooth_speed  is True


		else:
			# Pre-2023-11-14 - Speed Threshold Filtering, this doesn't appropriately preserve occupancy since just the spikes are filtered.
			# TODO: 2023-04-07 - CORRECTNESS ISSUE HERE. Interpolating the positions/speeds to the spikes and then filtering makes it difficult to determine the occupancy of each bin.
				# Kourosh and Kamran both process in terms of time bins.

			#TODO 2023-11-10 16:31 CORRECTNESS ISSUE: - [ ] When we filter on the speed_thresh and remove the spikes, we must also remove the time below this threshold from consideration!

			# Add interpolated velocity information to spikes dataframe:
			if 'speed' not in self._filtered_spikes_df.columns:
				self._filtered_spikes_df['speed'] = np.interp(self._filtered_spikes_df[spikes_df.spikes.time_variable_name].to_numpy(), self.filtered_pos_df.t.to_numpy(), self.speed) ## NOTE: self.speed is either the regular ['speed'] column of the position_df OR the 'speed_smooth'] column if self.should_smooth_speed  is True

			if self.config.speed_thresh is None:
				# No speed thresholding, all speeds allowed
				self._filtered_spikes_df = self._filtered_spikes_df
			else:
				# threshold by speed
				print(f'WARN: TODO 2023-11-10 16:31 CORRECTNESS ISSUE: - [ ] When we filter on the speed_thresh and remove the spikes, we must also remove the time below this threshold from consideration!')
				self._filtered_spikes_df = self._filtered_spikes_df[self._filtered_spikes_df['speed'] > self.config.speed_thresh]


		if debug_print:
			print(f'post speed filtering: {np.shape(self._filtered_spikes_df)[0]} spikes.')

		

		## Binning with Fixed bin size:
		# 2022-12-09 - We want to be able to have both long/short track placefields have the same bins.
		if (self.ndim > 1):
			if self.config.grid_bin_bounds is None:
				grid_bin_bounds = PlacefieldComputationParameters.compute_grid_bin_bounds(self.filtered_pos_df.x.to_numpy(), self.filtered_pos_df.y.to_numpy())
			else:
				# Use grid_bin_bounds:
				if ((self.config.grid_bin_bounds[0] is None) or (self.config.grid_bin_bounds[1] is None)):
					print(f'WARN: computing pf2D with set self.config.grid_bin_bounds but one of the compoenents is None! self.config.grid_bin_bounds: {self.config.grid_bin_bounds}.\n\trecomputing from positions and ignoring set grid_bin_bounds!')
					grid_bin_bounds = PlacefieldComputationParameters.compute_grid_bin_bounds(self.filtered_pos_df.x.to_numpy(), self.filtered_pos_df.y.to_numpy())
				else:
					if debug_print:
						print(f'using self.config.grid_bin_bounds: {self.config.grid_bin_bounds}')
					grid_bin_bounds = self.config.grid_bin_bounds
			x_range, y_range = grid_bin_bounds # unpack grid_bin_bounds

			self.xbin, self.ybin, self.bin_info = PfND._bin_pos_nD(x_range, y_range, bin_size=self.config.grid_bin) # bin_size mode
		else:
			# 1D case
			if self.config.grid_bin_bounds_1D is None:
				grid_bin_bounds_1D = PlacefieldComputationParameters.compute_grid_bin_bounds(self.filtered_pos_df.x.to_numpy(), None)[0]
			else:
				if debug_print:
					print(f'using self.config.grid_bin_bounds_1D: {self.config.grid_bin_bounds_1D}')
				grid_bin_bounds_1D = self.config.grid_bin_bounds_1D
			x_range = grid_bin_bounds_1D
			self.xbin, self.ybin, self.bin_info = PfND._bin_pos_nD(x_range, None, bin_size=self.config.grid_bin) # bin_size mode

		## Adds the 'binned_x' (and if 2D 'binned_y') columns to the position dataframe:
		if 'binned_x' not in self._filtered_pos_df.columns:
			self._filtered_pos_df, _, _, _ = PfND.build_position_df_discretized_binned_positions(self._filtered_pos_df, self.config, xbin_values=self.xbin, ybin_values=self.ybin, debug_print=False)


	def compute(self):
		""" actually compute the placefields after self.setup(...) is complete.


		Depends on:
			self.config.smooth
			self.x, self.y, self.xbin, self.ybin, self.position_srate


		Assigns:

			self.ratemap
			self.ratemap_spiketrains
			self.ratemap_spiketrains_pos

			self._included_thresh_neurons_indx
			self._peak_frate_filter_function

		"""
		# --- occupancy map calculation -----------
		if not self.should_smooth_spatial_occupancy_map:
			smooth_occupancy_map = (0.0, 0.0)
		else:
			smooth_occupancy_map = self.config.smooth
		if (self.ndim > 1):
			occupancy, unsmoothed_occupancy, xedges, yedges = Pf2D._compute_occupancy(self.x, self.y, self.xbin, self.ybin, self.position_srate, smooth_occupancy_map)
		else:
			occupancy, unsmoothed_occupancy, xedges = Pf1D._compute_occupancy(self.x, self.xbin, self.position_srate, smooth_occupancy_map[0])

		# Output lists, for compatibility with Pf1D and Pf2D:
		spk_pos, spk_t, spikes_maps, tuning_maps, unsmoothed_tuning_maps = [], [], [], [], []

		# Once filtering and binning is done, apply the grouping:
		# Group by the aclu (cluster indicator) column
		cell_grouped_spikes_df = self.filtered_spikes_df.groupby(['aclu'])
		cell_spikes_dfs = [cell_grouped_spikes_df.get_group(a_neuron_id) for a_neuron_id in self.filtered_spikes_df.spikes.neuron_ids] # a list of dataframes for each neuron_id

		# NOTE: regardless of whether should_smooth_final_tuning_map is true or not, we must pass in the actual smooth value to the _compute_tuning_map(...) function so it can choose to filter its firing map or not. Only if should_smooth_final_tuning_map is enabled will the final product be smoothed.

		# re-interpolate given the updated spks
		for cell_df in cell_spikes_dfs:
			# cell_spike_times = cell_df[spikes_df.spikes.time_variable_name].to_numpy()
			cell_spike_times = cell_df[self.filtered_spikes_df.spikes.time_variable_name].to_numpy()
			spk_x = np.interp(cell_spike_times, self.t, self.x) # TODO: shouldn't we already have interpolated spike times for all spikes in the dataframe?

			# update the dataframe 'x','speed' and 'y' properties:
			# cell_df.loc[:, 'x'] = spk_x
			# cell_df.loc[:, 'speed'] = spk_spd
			if (self.ndim > 1):
				spk_y = np.interp(cell_spike_times, self.t, self.y) # TODO: shouldn't we already have interpolated spike times for all spikes in the dataframe?
				# cell_df.loc[:, 'y'] = spk_y
				spk_pos.append([spk_x, spk_y])
				curr_cell_tuning_map, curr_cell_never_smoothed_tuning_map, curr_cell_spikes_map, curr_cell_unsmoothed_spikes_map = Pf2D._compute_tuning_map(spk_x, spk_y, self.xbin, self.ybin, occupancy, self.config.smooth, should_also_return_intermediate_spikes_map=self._save_intermediate_spikes_maps)

			else:
				# otherwise only 1D:
				spk_pos.append([spk_x])
				curr_cell_tuning_map, curr_cell_never_smoothed_tuning_map, curr_cell_spikes_map, curr_cell_unsmoothed_spikes_map = Pf1D._compute_tuning_map(spk_x, self.xbin, occupancy, self.config.smooth[0], should_also_return_intermediate_spikes_map=self._save_intermediate_spikes_maps)

			spk_t.append(cell_spike_times)
			tuning_maps.append(curr_cell_tuning_map)
			unsmoothed_tuning_maps.append(curr_cell_never_smoothed_tuning_map)
			spikes_maps.append(curr_cell_spikes_map)


		# ---- cells with peak frate abouve thresh
		self._included_thresh_neurons_indx, self._peak_frate_filter_function = PfND._build_peak_frate_filter(tuning_maps, self.config.frate_thresh)

		# there is only one tuning_map per neuron that means the thresh_neurons_indx:
		filtered_tuning_maps = np.asarray(self._peak_frate_filter_function(tuning_maps.copy()))
		filtered_unsmoothed_tuning_maps = np.asarray(self._peak_frate_filter_function(unsmoothed_tuning_maps.copy()))

		filtered_spikes_maps = self._peak_frate_filter_function(spikes_maps.copy())
		filtered_neuron_ids = self._peak_frate_filter_function(self.filtered_spikes_df.spikes.neuron_ids) 
		filtered_tuple_neuron_ids = self._peak_frate_filter_function(self.filtered_spikes_df.spikes.neuron_probe_tuple_ids) # the (shank, probe) tuples corresponding to neuron_ids

		self._ratemap = Ratemap(filtered_tuning_maps, unsmoothed_tuning_maps=filtered_unsmoothed_tuning_maps, spikes_maps=filtered_spikes_maps, xbin=self.xbin, ybin=self.ybin, neuron_ids=filtered_neuron_ids, occupancy=occupancy, neuron_extended_ids=filtered_tuple_neuron_ids)
		self.ratemap_spiketrains = self._peak_frate_filter_function(spk_t)
		self.ratemap_spiketrains_pos = self._peak_frate_filter_function(spk_pos)

	# PeakLocationRepresentingMixin + ContinuousPeakLocationRepresentingMixin conformances:
	@property
	def PeakLocationRepresentingMixin_peak_curves_variable(self) -> NDArray:
		""" the variable that the peaks are calculated and returned for """
		return self.ratemap.PeakLocationRepresentingMixin_peak_curves_variable
	
	@property
	def ContinuousPeakLocationRepresentingMixin_peak_curves_variable(self) -> NDArray:
		""" the variable that the peaks are calculated and returned for """
		return self.ratemap.ContinuousPeakLocationRepresentingMixin_peak_curves_variable
	


	@property
	def t(self) -> NDArray:
		"""The position timestamps property."""
		return self.filtered_pos_df.t.to_numpy()

	@property
	def x(self) -> NDArray:
		"""The position timestamps property."""
		return self.filtered_pos_df.x.to_numpy()

	@property
	def y(self) -> Optional[NDArray]:
		"""The position timestamps property."""
		if (self.ndim > 1):
			return self.filtered_pos_df.y.to_numpy()
		else:
			return None
	@property
	def speed(self) -> NDArray:
		"""The position timestamps property."""
		if (self.should_smooth_speed and (self.config.smooth is not None) and (self.config.smooth[0] > 0.0)):
			return self.filtered_pos_df.speed_smooth.to_numpy()
		else:
			return self.filtered_pos_df.speed.to_numpy()

	@property
	def xbin_centers(self):
		return self.xbin[:-1] + np.diff(self.xbin) / 2

	@property
	def ybin_centers(self):
		return self.ybin[:-1] + np.diff(self.ybin) / 2

	@property
	def filtered_spikes_df(self):
		"""The filtered_spikes_df property."""
		return self._filtered_spikes_df
	@filtered_spikes_df.setter
	def filtered_spikes_df(self, value):
		self._filtered_spikes_df = value

	@property
	def filtered_pos_df(self):
		"""The filtered_pos_df property."""
		return self._filtered_pos_df
	@filtered_pos_df.setter
	def filtered_pos_df(self, value):
		self._filtered_pos_df = value

	## ratemap read/write pass-through to private attributes
	@property
	def ratemap(self):
		"""The ratemap property."""
		return self._ratemap
	@ratemap.setter
	def ratemap(self, value):
		self._ratemap = value

	@property
	def ratemap_spiketrains(self):
		"""The ratemap_spiketrains property."""
		return self._ratemap_spiketrains
	@ratemap_spiketrains.setter
	def ratemap_spiketrains(self, value):
		self._ratemap_spiketrains = value

	@property
	def ratemap_spiketrains_pos(self):
		"""The ratemap_spiketrains_pos property."""
		return self._ratemap_spiketrains_pos
	@ratemap_spiketrains_pos.setter
	def ratemap_spiketrains_pos(self, value):
		self._ratemap_spiketrains_pos = value


	## ratemap convinence accessors
	@property
	def occupancy(self):
		"""The occupancy property."""
		return self.ratemap.occupancy
	@occupancy.setter
	def occupancy(self, value):
		self.ratemap.occupancy = value
	@property
	def never_visited_occupancy_mask(self):
		return self.ratemap.never_visited_occupancy_mask
	@property
	def nan_never_visited_occupancy(self):
		return self.ratemap.nan_never_visited_occupancy
	@property
	def probability_normalized_occupancy(self) -> NDArray:
		return self.ratemap.probability_normalized_occupancy
	@property
	def visited_occupancy_mask(self) -> NDArray:
		return self.ratemap.visited_occupancy_mask
	


	@property
	def neuron_extended_ids(self):
		"""The neuron_extended_ids property."""
		return self.ratemap.neuron_extended_ids
	@neuron_extended_ids.setter
	def neuron_extended_ids(self, value):
		self.ratemap.neuron_extended_ids = value

			
	@property
	def tuning_curves_dict(self) -> Dict[types.aclu_index, NDArray]:
		""" aclu:tuning_curve_array """
		return self.ratemap.tuning_curves_dict
	
	@property
	def normalized_tuning_curves_dict(self) -> Dict[types.aclu_index, NDArray]:
		""" aclu:tuning_curve_array """
		return self.ratemap.normalized_tuning_curves_dict
	
	
	## self.config convinence accessors. Mostly for compatibility with Pf1D and Pf2D
	@property
	def frate_thresh(self):
		"""The frate_thresh property."""
		return self.config.frate_thresh
	@property
	def speed_thresh(self):
		"""The speed_thresh property."""
		return self.config.speed_thresh

	@property
	def pos_bin_size(self) -> Union[float, Tuple[float, float]]:
		""" extracts pos_bin_size: the size of the x_bin in [cm], from the decoder. 
		
		returns a tuple if 2D or a single float if 1D

		"""
			# pos_bin_size: the size of the x_bin in [cm]
		if self.bin_info is not None:
			pos_x_bin_size = float(self.bin_info['xstep'])
			pos_y_bin_size = self.bin_info.get('ystep', None)
			if pos_y_bin_size is not None:
				return (pos_x_bin_size, float(pos_y_bin_size))
			else:
				# 1D
				return pos_x_bin_size
		else:
			## if the bin_info is for some reason not accessible, just average the distance between the bin centers.
			assert (self.xbin_centers is not None) and (len(self.xbin_centers) > 1)
			pos_x_bin_size = np.diff(self.xbin_centers).mean()
			if self.ybin_centers is not None:
				assert (self.ybin_centers is not None) and (len(self.ybin_centers) > 1)
				pos_y_bin_size = np.diff(self.ybin_centers).mean()
				return (pos_x_bin_size, float(pos_y_bin_size))
			else:
				# 1D
				return pos_x_bin_size

		
	
	@property
	def frate_filter_fcn(self):
		"""The frate_filter_fcn property."""
		return self._peak_frate_filter_function

	## dimensionality (ndim) helpers
	@property
	def _position_variable_names(self):
		"""The names of the position variables as determined by self.ndim."""
		if (self.ndim > 1):
			return ['x', 'y']
		else:
			return ['x']

	@property
	def included_neuron_IDXs(self):
		"""The neuron INDEXES, NOT IDs (not 'aclu' values) that were included after filtering by frate and etc. """
		return self._included_thresh_neurons_indx ## TODO: these are basically wrong, we should use self.ratemap.neuron_IDs instead!

	@property
	def included_neuron_IDs(self):
		"""The neuron IDs ('aclu' values) that were included after filtering by frate and etc. """
		return self._filtered_spikes_df.spikes.neuron_ids[self.included_neuron_IDXs] ## TODO: these are basically wrong, we should use self.ratemap.neuron_IDs instead!

	
		

	# for NeuronUnitSlicableObjectProtocol:
	def get_by_id(self, ids) -> "PfND":
		"""Implementors return a copy of themselves with neuron_ids equal to ids
			Needs to update: copy_pf._filtered_spikes_df, copy_pf.ratemap, copy_pf.ratemap_spiketrains, copy_pf.ratemap_spiketrains_pos, 
		"""
		copy_pf = deepcopy(self)
		# filter the spikes_df:
		copy_pf._filtered_spikes_df = copy_pf._filtered_spikes_df[np.isin(copy_pf._filtered_spikes_df.aclu, ids)]
		## Recompute:
		copy_pf.compute() # does recompute, updating: copy_pf.ratemap, copy_pf.ratemap_spiketrains, copy_pf.ratemap_spiketrains_pos, and more. TODO EFFICIENCY 2023-03-02 - This is overkill and I could filter the tuning_curves and etc directly, but this is easier for now. 
		return copy_pf


	def replacing_computation_epochs(self, epochs: Union[Epoch, pd.DataFrame]) -> "PfND":
		"""Implementors return a copy of themselves with their computation epochs replaced by the provided ones. The existing epochs are unrelated and do not need to be related to the new ones.
		"""
		new_epochs_obj: Epoch = Epoch(ensure_dataframe(deepcopy(epochs)).epochs.get_valid_df()).get_non_overlapping()
		copy_epoch_replaced_pf1D = deepcopy(self)
		return PfND(spikes_df=copy_epoch_replaced_pf1D.spikes_df, position=copy_epoch_replaced_pf1D.position, epochs=new_epochs_obj, config=deepcopy(copy_epoch_replaced_pf1D.config), compute_on_init=True)


	
		
	def conform_to_position_bins(self, target_pf, force_recompute=False):
		""" Allow overriding PfND's bins:
			# 2022-12-09 - We want to be able to have both long/short track placefields have the same spatial bins.
			This function standardizes the short pf1D's xbins to the same ones as the long_pf1D, and then recalculates it.
			Usage:
				short_pf1D, did_update_bins = short_pf1D.conform_to_position_bins(long_pf1D)
		"""
		did_update_bins = False
		if force_recompute or (len(self.xbin) < len(target_pf.xbin)) or ((self.ndim > 1) and (len(self.ybin) < len(target_pf.ybin))):
			print(f'self will be re-binned to match target_pf...')
			# bak_self = deepcopy(self) # Backup the original first
			xbin, ybin, bin_info, grid_bin = target_pf.xbin, target_pf.ybin, target_pf.bin_info, target_pf.config.grid_bin
			## Apply to the short dataframe:
			self.xbin, self.ybin, self.bin_info, self.config.grid_bin = xbin, ybin, bin_info, grid_bin
			## Updates (replacing) the 'binned_x' (and if 2D 'binned_y') columns to the position dataframe:
			self._filtered_pos_df, _, _, _ = PfND.build_position_df_discretized_binned_positions(self._filtered_pos_df, self.config, xbin_values=self.xbin, ybin_values=self.ybin, debug_print=False) # Finishes setup
			self.compute() # does compute
			print(f'done.') ## Successfully re-bins pf1D:
			did_update_bins = True # set the update flag
		else:
			# No changes needed:
			did_update_bins = False

		return self, did_update_bins

	def to_1D_maximum_projection(self) -> "PfND":
		return PfND.build_1D_maximum_projection(self)

	@classmethod
	def build_1D_maximum_projection(cls, pf2D: "PfND") -> "PfND":
		""" builds a 1D ratemap from a 2D ratemap
		creation_date='2023-04-05 14:02'

		Usage:
			ratemap_1D = build_1D_maximum_projection(ratemap_2D)
		"""
		assert pf2D.ndim > 1, f"ratemap_2D ndim must be greater than 1 (usually 2) but ndim: {pf2D.ndim}."
		# ratemap_1D_spikes_maps = np.nanmax(pf2D.spikes_maps, axis=-1) #.shape (n_cells, n_xbins)
		# ratemap_1D_tuning_curves = np.nanmax(pf2D.tuning_curves, axis=-1) #.shape (n_cells, n_xbins)
		# ratemap_1D_unsmoothed_tuning_maps = np.nanmax(pf2D.unsmoothed_tuning_maps, axis=-1) #.shape (n_cells, n_xbins)
		# ratemap_1D_occupancy = np.sum(pf2D.occupancy, axis=-1) #.shape (n_xbins,)
		new_pf1D = deepcopy(pf2D)
		new_pf1D.position.drop_dimensions_above(1) # drop dimensions above 1
		new_pf1D_ratemap = new_pf1D.ratemap.to_1D_maximum_projection()
		new_pf1D = PfND(spikes_df=new_pf1D.spikes_df, position=new_pf1D.position, epochs=new_pf1D.epochs, config=new_pf1D.config, position_srate=new_pf1D.position.sampling_rate,
			setup_on_init=True, compute_on_init=False,
			ratemap=new_pf1D_ratemap, ratemap_spiketrains=new_pf1D._ratemap_spiketrains,ratemap_spiketrains_pos=new_pf1D._ratemap_spiketrains_pos, filtered_pos_df=new_pf1D._filtered_pos_df, filtered_spikes_df=new_pf1D._filtered_spikes_df,
			ndim=1, xbin=new_pf1D.xbin, ybin=None, bin_info=None)

		# new_pf1D.ratemap = new_pf1D_ratemap
		# TODO: strip 2nd dimension (y-axis) from:
		# bin_info
		# position_df
		new_pf1D = cls._drop_extra_position_info(new_pf1D)
		# ratemap_1D = Ratemap(ratemap_1D_tuning_curves, unsmoothed_tuning_maps=ratemap_1D_unsmoothed_tuning_maps, spikes_maps=ratemap_1D_spikes_maps, xbin=pf2D.xbin, ybin=None, occupancy=ratemap_1D_occupancy, neuron_ids=deepcopy(pf2D.neuron_ids), neuron_extended_ids=deepcopy(pf2D.neuron_extended_ids), metadata=pf2D.metadata)
		return new_pf1D




	@classmethod
	def filtered_by_speed(cls, epochs_df: pd.DataFrame, position_df: pd.DataFrame, speed_thresh: Optional[float], speed_column_override_name:Optional[str]=None, debug_print:bool=False):
		""" Filters the position_df by speed and epoch_df correctly, so we can get actual occupancy.
		2023-11-14 - 
		
		
		speed_thresh = a_decoder.config.speed_thresh # 10.0
		position_df = a_decoder.position.to_dataframe()
		
		
		"""
		from neuropy.utils.mixins.time_slicing import add_epochs_id_identity

		if debug_print:
			pre_filtering_duration = epochs_df.duration.sum()
			print(f'pre_filtering_duration: {pre_filtering_duration}') # 135.73698799998965

		epoch_id_string: str = f'decoder_epoch_id'
		position_df = add_epochs_id_identity(position_df, epochs_df, epoch_id_key_name=epoch_id_string, epoch_label_column_name=None, no_interval_fill_value=-1, override_time_variable_name='t')
		# drop the -1 indicies because they are below the speed:
		position_df = position_df[position_df[epoch_id_string] != -1] # Drop all non-included spikes
				
		if debug_print:
			n_pre_filtered_samples = len(position_df)
			print(f'n_pre_filtered_samples: {n_pre_filtered_samples}')

		if speed_thresh is not None:
			if speed_column_override_name is None:
				speed_column_name = 'speed'
			else:
				speed_column_name = speed_column_override_name
			position_df = position_df[position_df[speed_column_name] < speed_thresh] # filter the samples by speed_thresh
		else:
			pass # position_df is just itself
		# Performed 3 aggregations grouped on column: 'decoder_epoch_id'
		speed_filtered_epochs_df = position_df.groupby(['decoder_epoch_id']).agg(t_first=('t', 'first'), t_last=('t', 'last'), t_count=('t', 'count')).reset_index()
		# Rename column 't_first' to 'start'
		speed_filtered_epochs_df = speed_filtered_epochs_df.rename(columns={'t_first': 'start'})
		# Rename column 't_last' to 'stop'
		speed_filtered_epochs_df = speed_filtered_epochs_df.rename(columns={'t_last': 'stop'})
		# Rename column 'decoder_epoch_id' to 'label'
		speed_filtered_epochs_df = speed_filtered_epochs_df.rename(columns={'decoder_epoch_id': 'label'})
		# Rename column 't_count' to 'n_samples'
		speed_filtered_epochs_df = speed_filtered_epochs_df.rename(columns={'t_count': 'n_samples'})
		speed_filtered_epochs_df['duration'] = speed_filtered_epochs_df['stop'] - speed_filtered_epochs_df['start']
		
		if debug_print:    
			post_filtering_duration = speed_filtered_epochs_df.duration.sum()
			print(f'post_filtering_duration: {post_filtering_duration}') # 130.96321400045417
		return speed_filtered_epochs_df





	def str_for_filename(self, prefix_string=''):
		if self.ndim <= 1:
			return '-'.join(['pf1D', f'{prefix_string}{self.config.str_for_filename(False)}'])
		else:
			return '-'.join(['pf2D', f'{prefix_string}{self.config.str_for_filename(True)}'])

	def str_for_display(self, prefix_string=''):
		if self.ndim <= 1:
			return '-'.join(['pf1D', f'{prefix_string}{self.config.str_for_display(False)}', f'cell_{curr_cell_id:02d}'])
		else:
			return '-'.join(['pf2D', f'{prefix_string}{self.config.str_for_display(True)}', f'cell_{curr_cell_id:02d}'])

	def to_dict(self):
		# Excluded from serialization: ['_included_thresh_neurons_indx', '_peak_frate_filter_function']
		# filter_fn = filters.exclude(fields(PfND)._included_thresh_neurons_indx, int)
		filter_fn = lambda attr, value: attr.name not in ["_included_thresh_neurons_indx", "_peak_frate_filter_function"]
		return asdict(self, filter=filter_fn) # serialize using attrs.asdict but exclude the listed properties

	## For serialization/pickling:
	def __getstate__(self):
		return self.to_dict()

	def __setstate__(self, state):
		""" assumes state is a dict generated by calling self.__getstate__() previously"""
		# print(f'__setstate__(self: {self}, state: {state})')
		# print(f'__setstate__(...): {list(self.__dict__.keys())}')
		self.__dict__ = state # set the dict
		self._save_intermediate_spikes_maps = True # False is not yet implemented
		# # Set the particulars if needed
		# self.config = state.get('config', None)
		# self.position_srate = state.get('position_srate', None)
		# self.ndim = state.get('ndim', None)
		# self.xbin = state.get('xbin', None)
		# self.ybin = state.get('ybin', None)
		# self.bin_info = state.get('bin_info', None)
		# ## The _included_thresh_neurons_indx and _peak_frate_filter_function are None:
		self._included_thresh_neurons_indx = None
		self._peak_frate_filter_function = None


	@staticmethod
	def _build_peak_frate_filter(tuning_maps, frate_thresh, debug_print=False):
		""" Finds the peak value of the tuning map for each cell and compares it to the frate_thresh to see if it should be included.

		Returns:
			thresh_neurons_indx: the list of indicies that meet the peak firing rate threshold critiera
			filter_function: a function that takes any list of length n_neurons (original number of neurons) and just indexes its passed list argument by thresh_neurons_indx (including only neurons that meet the thresholding criteria)
		"""
		# ---- cells with peak frate abouve thresh ------
		n_neurons = len(tuning_maps)

		if debug_print:
			print('_build_peak_frate_filter(...):')
			print('\t frate_thresh: {}'.format(frate_thresh))
			print('\t n_neurons: {}'.format(n_neurons))

		max_neurons_firing_rates = [np.nanmax(tuning_maps[neuron_indx]) for neuron_indx in range(n_neurons)]
		if debug_print:
			print(f'max_neurons_firing_rates: {max_neurons_firing_rates}')

		# only include the indicies that have a max firing rate greater than frate_thresh
		included_thresh_neurons_indx = [
			neuron_indx
			for neuron_indx in range(n_neurons)
			if np.nanmax(tuning_maps[neuron_indx]) > frate_thresh
		]
		if debug_print:
			print('\t thresh_neurons_indx: {}'.format(included_thresh_neurons_indx))
		# filter_function: just indexes its passed list argument by thresh_neurons_indx (including only neurons that meet the thresholding criteria)
		filter_function = lambda list_: [list_[_] for _ in included_thresh_neurons_indx] # filter_function: takes any list of length n_neurons (original number of neurons) and returns only the elements that met the firing rate criteria

		return included_thresh_neurons_indx, filter_function

	@staticmethod
	def _bin_pos_nD(x: np.ndarray, y: np.ndarray, num_bins=None, bin_size=None):
		""" Spatially bins the provided x and y vectors into position bins based on either the specified num_bins or the specified bin_size
		Usage:
			## Binning with Fixed Number of Bins:
			xbin, ybin, bin_info = _bin_pos(pos_df.x.to_numpy(), pos_df.y.to_numpy(), bin_size=active_config.computation_config.grid_bin) # bin_size mode
			print(bin_info)
			## Binning with Fixed Bin Sizes:
			xbin, ybin, bin_info = _bin_pos(pos_df.x.to_numpy(), pos_df.y.to_numpy(), num_bins=num_bins) # num_bins mode
			print(bin_info)

		TODO: 2022-04-22 - Note that I discovered that the bins generated here might cause an error when used with Pandas .cut function, which does not include the left (most minimum) values by default. This would cause the minimumal values not to be included.
		"""
		return bin_pos_nD(x, y, num_bins=num_bins, bin_size=bin_size)


	## Binned Position Columns:
	@staticmethod
	def build_position_df_discretized_binned_positions(active_pos_df, active_computation_config, xbin_values=None, ybin_values=None, debug_print=False):
		""" Adds the 'binned_x' and 'binned_y' columns to the position dataframe

		Assumes either 1D or 2D positions dependent on whether the 'y' column exists in active_pos_df.columns.
		Wraps the build_df_discretized_binned_position_columns and appropriately unwraps the result for compatibility with previous implementations.

		"""
		# If xbin_values is not None and ybin_values is None, assume 1D
		# if xbin_values is not None and ybin_values is None:
		if 'y' not in active_pos_df.columns:
			# Assume 1D:
			ndim = 1
			pos_col_names = ('x',)
			binned_col_names = ('binned_x',)
			bin_values = (xbin_values,)
		else:
			# otherwise assume 2D:
			ndim = 2
			pos_col_names = ('x', 'y')
			binned_col_names = ('binned_x', 'binned_y')
			bin_values = (xbin_values, ybin_values)

		# bin the dataframe's x and y positions into bins, with binned_x and binned_y containing the index of the bin that the given position is contained within.
		active_pos_df, out_bins, bin_info = build_df_discretized_binned_position_columns(active_pos_df, bin_values=bin_values, position_column_names=pos_col_names, binned_column_names=binned_col_names, active_computation_config=active_computation_config, force_recompute=False, debug_print=debug_print)

		if ndim == 1:
			# Assume 1D:
			xbin = out_bins[0]
			ybin = None
		else:
			(xbin, ybin) = out_bins

		return active_pos_df, xbin, ybin, bin_info

	@classmethod
	def _drop_extra_position_info(cls, pf):
		""" if pf is 1D (as indicated by `pf.ndim`), drop any 'y' related columns. """
		if (pf.ndim < 2):
			# Drop any 'y' related columns if it's a 1D version:
			# print(f"dropping 'y'-related columns in pf._filtered_spikes_df because pf.ndim: {pf.ndim} (< 2).")
			columns_to_drop = [col for col in ['y', 'y_loaded'] if col in pf._filtered_spikes_df.columns]
			pf._filtered_spikes_df.drop(columns=columns_to_drop, inplace=True)

		pf._filtered_pos_df.dropna(axis=0, how='any', subset=[*pf._position_variable_names], inplace=True) # dropped NaN values
		
		if 'binned_x' in pf._filtered_pos_df:
			if (pf.position.ndim > 1):
				pf._filtered_pos_df.dropna(axis=0, how='any', subset=['binned_x', 'binned_y'], inplace=True) # dropped NaN values
			else:
				pf._filtered_pos_df.dropna(axis=0, how='any', subset=['binned_x'], inplace=True) # dropped NaN values
		return pf

	# HDFMixin Conformances ______________________________________________________________________________________________ #
	def to_hdf(self, file_path, key: str, **kwargs):
		""" Saves the object to key in the hdf5 file specified by file_path
		Usage:
			hdf5_output_path: Path = curr_active_pipeline.get_output_path().joinpath('test_data.h5')
			_pfnd_obj: PfND = long_one_step_decoder_1D.pf
			_pfnd_obj.to_hdf(hdf5_output_path, key='test_pfnd')
		"""
	
		self.position.to_hdf(file_path=file_path, key=f'{key}/pos')
		if self.epochs is not None:
			self.epochs.to_hdf(file_path=file_path, key=f'{key}/epochs') #TODO 2023-07-30 11:13: - [ ] What if self.epochs is None?
		else:
			# if self.epochs is None
			pass
		self.spikes_df.spikes.to_hdf(file_path, key=f'{key}/spikes')
		self.ratemap.to_hdf(file_path, key=f'{key}/ratemap')

		# Open the file with h5py to add attributes to the group. The pandas.HDFStore object doesn't provide a direct way to manipulate groups as objects, as it is primarily intended to work with datasets (i.e., pandas DataFrames)
		with h5py.File(file_path, 'r+') as f:
			## Unfortunately, you cannot directly assign a dictionary to the attrs attribute of an h5py group or dataset. The attrs attribute is an instance of a special class that behaves like a dictionary in some ways but not in others. You must assign attributes individually
			group = f[key]
			group.attrs['position_srate'] = self.position_srate
			group.attrs['ndim'] = self.ndim

			# can't just set the dict directly
			# group.attrs['config'] = str(self.config.to_dict())  # Store as string if it's a complex object
			# Manually set the config attributes
			config_dict = self.config.to_dict()
			group.attrs['config/speed_thresh'] = config_dict['speed_thresh']
			group.attrs['config/grid_bin'] = config_dict['grid_bin']
			group.attrs['config/grid_bin_bounds'] = config_dict['grid_bin_bounds']
			group.attrs['config/smooth'] = config_dict['smooth']
			group.attrs['config/frate_thresh'] = config_dict['frate_thresh']


	@classmethod
	def read_hdf(cls, file_path, key: str, **kwargs) -> "PfND":
		""" Reads the data from the key in the hdf5 file at file_path
		Usage:
			_reread_pfnd_obj = PfND.read_hdf(hdf5_output_path, key='test_pfnd')
			_reread_pfnd_obj
		"""
		# Read DataFrames using pandas
		position = Position.read_hdf(file_path, key=f'{key}/pos')
		try:
			epochs = Epoch.read_hdf(file_path, key=f'{key}/epochs')
		except KeyError as e:
			# epochs can be None, in which case the serialized object will not contain the f'{key}/epochs' key.  'No object named test_pfnd/epochs in the file'
			epochs = None
		except Exception as e:
			# epochs can be None, in which case the serialized object will not contain the f'{key}/epochs' key
			print(f'Unhandled exception {e}')
			raise e
		
		spikes_df = SpikesAccessor.read_hdf(file_path, key=f'{key}/spikes')

		# Open the file with h5py to read attributes
		with h5py.File(file_path, 'r') as f:
			group = f[key]
			position_srate = group.attrs['position_srate']
			ndim = group.attrs['ndim'] # Assuming you'll use it somewhere else if needed

			# Read the config attributes
			config_dict = {
				'speed_thresh': group.attrs['config/speed_thresh'],
				'grid_bin': tuple(group.attrs['config/grid_bin']),
				'grid_bin_bounds': tuple(group.attrs['config/grid_bin_bounds']),
				'smooth': tuple(group.attrs['config/smooth']),
				'frate_thresh': group.attrs['config/frate_thresh']
			}

		# Create a PlacefieldComputationParameters object from the config_dict
		config = PlacefieldComputationParameters(**config_dict)

		# Reconstruct the object using the from_config_values class method
		return cls(spikes_df=spikes_df, position=position, epochs=epochs, config=config, position_srate=position_srate)
	

	@classmethod
	def build_pseduo_2D_directional_placefield_positions(cls, *directional_1D_decoder_list) -> Position:
		""" 2023-11-10 - builds the positions for the directional 1D decoders into a pseudo 2D decoder
		## HACK: this adds the two directions of two separate 1D placefields into a stack with a pseudo-y dimension (with two bins):
		## WARNING: the animal will "teleport" between y-coordinates between the RL/LR laps. This will mean that all velocity_y, or vector-based velocity calculations (that use both x and y) are going to be messed up.
		
		First decoder is assigned virtual y-positions: 1.0
		Second decoder is assigned virtual y-positions: 2.0,
		etc.
		
		"""
		# positions merge:
		position = Position(pd.concat([a_decoder.position.to_dataframe() for a_decoder in directional_1D_decoder_list]).sort_values('t').drop_duplicates(subset=['t'], inplace=False))
		position.df['y'] = -1.0 # was 0.0, but we iterate through the whole list, so this would skip zero
		## Add the epoch ids to each spike so we can easily filter on them:
		for i, a_decoder in enumerate(directional_1D_decoder_list):        
			epoch_id_string: str = f'decoder_{i}_epoch_id'
			position.df = add_epochs_id_identity(position.df, a_decoder.epochs.to_dataframe(), epoch_id_key_name=epoch_id_string, epoch_label_column_name=None, no_interval_fill_value=-1, override_time_variable_name='t')
			position.df.loc[(position.df[epoch_id_string] != -1), 'y'] = float(i+1)
		# TODO: so the valid y-values should be [1, ..., len(directional_1D_decoder_list)]
		return position
		

	@classmethod
	def _DEP_build_merged_directional_placefields(cls, *directional_1D_decoder_list, debug_print = True) -> "PfND": # , lhs: "PfND", rhs: "PfND"
		""" 2023-11-10 - Combine the non-directional PDFs and renormalize to get the directional PDF:
		 
		First decoder is assigned virtual y-positions: 1.0
		Second decoder is assigned virtual y-positions: 2.0,
		etc.
		
		Usage:
			from neuropy.analyses.placefields import PfND

			## Combine the non-directional PDFs and renormalize to get the directional PDF:
			# Inputs: long_LR_pf1D, long_RL_pf1D
			merged_pf1D = PfND.build_merged_directional_placefields(deepcopy(long_LR_pf1D), deepcopy(long_RL_pf1D), debug_print = True)
			merged_pf1D 

		"""
		
		from neuropy.analyses.placefields import PlacefieldComputationParameters

		assert len(directional_1D_decoder_list) > 0
		lhs = directional_1D_decoder_list[0] # first decoder
		remaining_decoder_list = directional_1D_decoder_list[1:]

		for rhs in remaining_decoder_list:
			assert np.all(lhs.xbin == rhs.xbin)
			assert np.all(lhs.ybin == rhs.ybin)
		xbin = lhs.xbin
		ybin = lhs.ybin
		for rhs in remaining_decoder_list:
			assert np.all(lhs.ndim == rhs.ndim)
		ndim = lhs.ndim
		new_pseduo_ndim = ndim + 1
		new_pseudo_num_ybins: int = len(directional_1D_decoder_list) # number of y-bins we'll need
		if debug_print:
			print(f'ndim: {ndim}, new_pseduo_ndim: {new_pseduo_ndim}\n\tnew_pseudo_num_ybins: {new_pseudo_num_ybins}')
			
		assert ndim == 1, f"currently only works for ndim == 1 but ndim: {ndim}! ybin will need to be changed to zbin for higher-order than 1D initial decoders."
		ybin = np.arange(new_pseudo_num_ybins + 1) # [0, 1, 2] because they are the edges of the bins
		if debug_print:
			print(f'ybin: {ybin}')
		for rhs in remaining_decoder_list:
			assert np.isclose(lhs.position_srate, rhs.position_srate, 0.01)
		position_srate = lhs.position_srate

		## Pre-computation variables:
		# These variables below are pre-computation variables and are used by `PfND.compute()` to actually build the ratemaps and filtered versions. They aren't quite right as is.
		# epochs are merged:
		epochs: Epoch = Epoch(pd.concat([a_decoder.epochs.to_dataframe() for a_decoder in directional_1D_decoder_list], ignore_index=True, verify_integrity=True).sort_values(['start', 'stop']))
		
		# spikes_df are merged:
		time_variable_name:str = lhs.spikes_df.spikes.time_variable_name
		spikes_df = pd.concat([a_decoder.spikes_df for a_decoder in directional_1D_decoder_list]).sort_values([time_variable_name, 'aclu']).drop_duplicates(subset=[time_variable_name, 'aclu'], inplace=False) # make sure this drops duplicates in (time_variable_name, 'aclu')

		# positions merge:
		position = cls.build_pseduo_2D_directional_placefield_positions(*directional_1D_decoder_list)
		
		# Make the needed modifications to the config so spatial smoothing isn't used on the pseduo-y dimension:
		# config: <PlacefieldComputationParameters: {'speed_thresh': 10.0, 'grid_bin': (3.793023081021702, 1.607897707662558), 'grid_bin_bounds': ((29.16, 261.7), (130.23, 150.99)), 'smooth': (2.0, 2.0), 'frate_thresh': 1.0};>
		config = deepcopy(lhs.config)
		config.is_directional = True
		config.grid_bin = (*config.grid_bin[:ndim], 1.0) # bin size is exactly one (because there will be two pseduo-dimensions)
		config.smooth = (*config.smooth[:ndim], 0.0) # do not allow smooth along the pseduo-y direction
		config.grid_bin_bounds = (*config.grid_bin_bounds[:ndim], (0, new_pseudo_num_ybins))
		# config # result: <PlacefieldComputationParameters: {'speed_thresh': 10.0, 'grid_bin': (3.793023081021702, 1.0), 'grid_bin_bounds': ((29.16, 261.7), (0, 2)), 'smooth': (2.0, None), 'frate_thresh': 1.0, 'is_directional': True};>
		merged_pf = PfND(spikes_df=spikes_df, position=position, epochs=epochs, config=config, position_srate=position_srate, xbin=xbin, ybin=ybin) # , ybin=
		return merged_pf


	@classmethod
	def build_merged_directional_placefields(cls, input_unidirectional_decoder_dict, debug_print = True) -> "PfND":
		""" 2024-01-02 - Combine the non-directional PDFs and renormalize to get the directional PDF:

		Builds a manually merged directional pf from a dict of pf1Ds (one for each direction)

		First decoder is assigned virtual y-positions: 1.0
		Second decoder is assigned virtual y-positions: 2.0,
		etc.


		@#TODO 2024-04-05 22:20: - [ ] The returned combined PfND is missing its `spikes_df`, `filtered_spikes_df` properties making `.get_by_id(...)` not work at all. Also `.extended_neuron_ids` is a list instead of a NDArray, making indexing into that fail in certain places.
		
		Usage:
			from neuropy.analyses.placefields import PfND

			## Combine the non-directional PDFs and renormalize to get the directional PDF:
			# Inputs: long_LR_pf1D, long_RL_pf1D
			merged_pf1D = PfND.build_merged_directional_placefields(deepcopy(long_LR_pf1D), deepcopy(long_RL_pf1D), debug_print = True)
			merged_pf1D 

		"""
			
		from neuropy.utils.indexing_helpers import union_of_arrays
		from neuropy.core.ratemap import Ratemap


		def _subfn_manual_pdf_merge_directional_pf1Ds(input_unidirectional_decoder_dict, debug_print):
			## Reset to no frate filter so the same cells are included for each decoder
			unidirectional_decoder_dict = deepcopy(input_unidirectional_decoder_dict)
			
			for a_decoder_name, a_decoder in unidirectional_decoder_dict.items():
				# a_filter_reset_decoder = deepcopy(a_decoder)
				original_num_neurons: int = a_decoder.ratemap.n_neurons
				if debug_print:
					print(f'original_num_neurons: {original_num_neurons}')
				a_decoder.config.frate_thresh = 0.0
				a_decoder.compute()
				post_filter_clear_num_neurons: int = a_decoder.ratemap.n_neurons
				if debug_print:
					print(f'post_filter_clear_num_neurons: {post_filter_clear_num_neurons}')
				
			unidirectional_ratemap_dict = {k:v.ratemap for k, v in unidirectional_decoder_dict.items()}


			xbins = {k:v.xbin for k, v in unidirectional_ratemap_dict.items()}
			neuron_ids = {k:v.neuron_ids for k, v in unidirectional_ratemap_dict.items()}
			pdf_normalized_tuning_curves = {k:v.pdf_normalized_tuning_curves for k, v in unidirectional_ratemap_dict.items()}
			occupancy_dict = {k:v.occupancy for k, v in unidirectional_ratemap_dict.items()}


			at_least_one_decoder_neuron_ids = union_of_arrays(*list(neuron_ids.values()))
			at_least_one_decoder_n_neurons = len(at_least_one_decoder_neuron_ids)
			at_least_one_decoder_neuron_ids_index_dict = dict(zip(at_least_one_decoder_neuron_ids, np.arange(len(at_least_one_decoder_neuron_ids))))
			at_least_one_decoder_neuron_extended_ids = {aclu:None for aclu, v in at_least_one_decoder_neuron_ids_index_dict.items()} # initialize to empty for each aclu
			
			# at_least_one_decoder_pdf_normalized_tuning_curves_dict = {}
			at_least_one_decoder_all_results_dict = {}


			for a_decoder_name, a_ratemap in unidirectional_ratemap_dict.items():
				n_xbin_centers = np.shape(a_ratemap.xbin_centers)[0]
				
				## Just builds the pdf_normalized_tuning_curves for this decoder with the correct size (including zeros for neuron_ids not present in this ratemap:
				if debug_print:
					print(f'at_least_one_decoder_n_neurons: {at_least_one_decoder_n_neurons}')
					print(f'n_xbin_centers: {n_xbin_centers}')
				a_ratemap_full_spikes_maps = np.full((at_least_one_decoder_n_neurons, n_xbin_centers), fill_value=0)
				a_ratemap_full_unsmoothed_tuning_curves = np.full((at_least_one_decoder_n_neurons, n_xbin_centers), fill_value=0.0)
				
				a_ratemap_full_tuning_curves = np.full((at_least_one_decoder_n_neurons, n_xbin_centers), fill_value=0.0)
				a_ratemap_full_pdf_normalized_tuning_curves = np.full((at_least_one_decoder_n_neurons, n_xbin_centers), fill_value=0.0)
				
				for aclu, aclu_extended_id, spikes_map, unsmoothed_tuning_map, tuning_curve, pdf_norm_curve in zip(a_ratemap.neuron_ids, a_ratemap.neuron_extended_ids, a_ratemap.spikes_maps, a_ratemap.unsmoothed_tuning_maps, a_ratemap.tuning_curves, a_ratemap.pdf_normalized_tuning_curves):
					curr_fragile_IDX = at_least_one_decoder_neuron_ids_index_dict[aclu]
					
					a_ratemap_full_spikes_maps[curr_fragile_IDX, :] = spikes_map
					a_ratemap_full_unsmoothed_tuning_curves[curr_fragile_IDX, :] = unsmoothed_tuning_map
					a_ratemap_full_tuning_curves[curr_fragile_IDX, :] = tuning_curve
					a_ratemap_full_pdf_normalized_tuning_curves[curr_fragile_IDX, :] = pdf_norm_curve
					
					if at_least_one_decoder_neuron_extended_ids[aclu] is None:
						# use this ratemaps neuron_extended_id for this aclu
						at_least_one_decoder_neuron_extended_ids[aclu] = aclu_extended_id # a_ratemap.neuron_extended_ids[a_ratemap_relative_aclu_fragile_linear_idx]
					
				# at_least_one_decoder_pdf_normalized_tuning_curves_dict[a_decoder_name] = a_ratemap_full_pdf_normalized_tuning_curves
				at_least_one_decoder_all_results_dict[a_decoder_name] = {'spikes_maps': a_ratemap_full_spikes_maps, 'unsmoothed_tuning_maps': a_ratemap_full_unsmoothed_tuning_curves, 'tuning_curves': a_ratemap_full_tuning_curves,
																		'pdf_normalized_tuning_curves': a_ratemap_full_pdf_normalized_tuning_curves}    # , 'a_ratemap_full_spikes_maps': a_ratemap_full_spikes_maps

			return unidirectional_decoder_dict, unidirectional_ratemap_dict, at_least_one_decoder_all_results_dict, at_least_one_decoder_neuron_ids, at_least_one_decoder_neuron_extended_ids


		# BEGIN MAIN FUNCTION BODY ___________________________________________________________________________________________ #

		directional_1D_decoder_dict, unidirectional_ratemap_dict, at_least_one_decoder_all_results_dict, at_least_one_decoder_neuron_ids, at_least_one_decoder_neuron_extended_ids = _subfn_manual_pdf_merge_directional_pf1Ds(input_unidirectional_decoder_dict, debug_print=debug_print)
		directional_1D_decoder_list = list(directional_1D_decoder_dict.values())
		
		assert len(directional_1D_decoder_list) > 0
		lhs = directional_1D_decoder_list[0] # first decoder
		remaining_decoder_list = directional_1D_decoder_list[1:]

		for rhs in remaining_decoder_list:
			assert np.all(lhs.xbin == rhs.xbin)
			assert np.all(lhs.ybin == rhs.ybin)
		xbin = lhs.xbin
		ybin = lhs.ybin
		for rhs in remaining_decoder_list:
			assert np.all(lhs.ndim == rhs.ndim)
		ndim = lhs.ndim
		new_pseduo_ndim = ndim + 1
		new_pseudo_num_ybins: int = len(directional_1D_decoder_list) # number of y-bins we'll need
		if debug_print:
			print(f'ndim: {ndim}, new_pseduo_ndim: {new_pseduo_ndim}\n\tnew_pseudo_num_ybins: {new_pseudo_num_ybins}')
			
		assert ndim == 1, f"currently only works for ndim == 1 but ndim: {ndim}! ybin will need to be changed to zbin for higher-order than 1D initial decoders."
		ybin = np.arange(new_pseudo_num_ybins + 1) # [0, 1, 2] because they are the edges of the bins
		if debug_print:
			print(f'ybin: {ybin}')
		for rhs in remaining_decoder_list:
			assert np.isclose(lhs.position_srate, rhs.position_srate, 0.01)
		position_srate = lhs.position_srate

		# stacked_pdf = np.stack(list(at_least_one_decoder_pdf_normalized_tuning_curves_dict.values()), axis=-1) # .shape (n_neurons, n_xbins, n_ybins): (80, 62, 2)
		
		# stacked_results_dict = {np.stack(list(at_least_one_decoder_all_results_dict.values()), axis=-1) for k,v in at_least_one_decoder_all_results_dict.items()} # .shape (n_neurons, n_xbins, n_ybins): (80, 62, 2)

		stacked_results_dict = {a_value_key:np.stack([v[a_value_key] for v in list(at_least_one_decoder_all_results_dict.values())], axis=-1) for a_value_key in ['tuning_curves', 'unsmoothed_tuning_maps', 'spikes_maps']}
		stacked_occupancy = np.stack([v.occupancy for k, v in directional_1D_decoder_dict.items()], axis=-1) # .shape: (62, 2)

		# normalized_stacked_pdf = stacked_pdf / np.sum(stacked_pdf, axis=-1, keepdims=True)
		# normalized_stacked_pdf

		new_ratemap = Ratemap(tuning_curves=stacked_results_dict['tuning_curves'], unsmoothed_tuning_maps=stacked_results_dict['unsmoothed_tuning_maps'], spikes_maps=stacked_results_dict['spikes_maps'],
							xbin=xbin, ybin=ybin, occupancy=stacked_occupancy, neuron_ids=at_least_one_decoder_neuron_ids, neuron_extended_ids=list(at_least_one_decoder_neuron_extended_ids.values())) # #TODO 2024-04-05 22:17: - [ ] This is where the ratemap's neuron_extended_ids is becoming a list
		
		## Pre-computation variables:
		# These variables below are pre-computation variables and are used by `PfND.compute()` to actually build the ratemaps and filtered versions. They aren't quite right as is.
		# epochs are merged:
		# epochs: Epoch = Epoch(pd.concat([a_decoder.epochs.to_dataframe() for a_decoder in directional_1D_decoder_list], ignore_index=True, verify_integrity=True).sort_values(['start', 'stop']))
		
		# spikes_df are merged:
		time_variable_name:str = lhs.spikes_df.spikes.time_variable_name
		# spikes_df = pd.concat([a_decoder.spikes_df for a_decoder in directional_1D_decoder_list]).sort_values([time_variable_name, 'aclu']).drop_duplicates(subset=[time_variable_name, 'aclu'], inplace=False) # make sure this drops duplicates in (time_variable_name, 'aclu')

		# positions merge:
		# position = cls.build_pseduo_2D_directional_placefield_positions(*directional_1D_decoder_list)
		
		# Make the needed modifications to the config so spatial smoothing isn't used on the pseduo-y dimension:
		# config: <PlacefieldComputationParameters: {'speed_thresh': 10.0, 'grid_bin': (3.793023081021702, 1.607897707662558), 'grid_bin_bounds': ((29.16, 261.7), (130.23, 150.99)), 'smooth': (2.0, 2.0), 'frate_thresh': 1.0};>
		config = deepcopy(lhs.config)
		config.is_directional = True
		config.grid_bin = (*config.grid_bin[:ndim], 1.0) # bin size is exactly one (because there will be two pseduo-dimensions)
		config.smooth = (*config.smooth[:ndim], 0.0) # do not allow smooth along the pseduo-y direction
		config.grid_bin_bounds = (*config.grid_bin_bounds[:ndim], (0, new_pseudo_num_ybins))
		# config # result: <PlacefieldComputationParameters: {'speed_thresh': 10.0, 'grid_bin': (3.793023081021702, 1.0), 'grid_bin_bounds': ((29.16, 261.7), (0, 2)), 'smooth': (2.0, None), 'frate_thresh': 1.0, 'is_directional': True};>
		merged_pf = PfND(spikes_df=None, position=None, epochs=None, config=config, position_srate=position_srate, xbin=xbin, ybin=ybin, ndim=new_pseduo_ndim,
					setup_on_init=False, compute_on_init=False) # , ybin=  #TODO 2024-04-05 22:19: - [ ] This is where the `spikes_df` (and thus `_filtered_spikes_df`) is being set to None
		merged_pf._ratemap = new_ratemap
		
		return merged_pf



	@classmethod
	def determine_pf_aclus_filtered_by_frate_and_qclu(cls, pf_dict: Dict[str, "PfND"], minimum_inclusion_fr_Hz:Optional[float]=None, included_qclu_values:Optional[List]=None):
		""" Filters the included neuron_ids by their `tuning_curve_unsmoothed_peak_firing_rates` (a property of their `.pf.ratemap`)
		minimum_inclusion_fr_Hz: float = 5.0
		modified_long_LR_decoder = filtered_by_frate(track_templates.long_LR_decoder, minimum_inclusion_fr_Hz=minimum_inclusion_fr_Hz, debug_print=True)

		individual_decoder_filtered_aclus_list: list of four lists of aclus, not constrained to have the same aclus as its long/short pair

		Usage:
			filtered_decoder_dict, filtered_direction_shared_aclus_list = PfND.determine_pf_aclus_filtered_by_frate_and_qclu(pf_dict=track_templates.get_pf_dict(), minimum_inclusion_fr_Hz=minimum_inclusion_fr_Hz, included_qclu_values=included_qclu_values)

		"""
		decoder_names = list(pf_dict.keys()) # ('long_LR', 'long_RL', 'short_LR', 'short_RL')
		modified_neuron_ids_dict = cls._perform_determine_pf_aclus_filtered_by_qclu_and_frate(pf_dict=pf_dict, minimum_inclusion_fr_Hz=minimum_inclusion_fr_Hz, included_qclu_values=included_qclu_values)
		# individual_decoder_filtered_aclus_list = list(modified_neuron_ids_dict.values())
		individual_pf_filtered_aclus_list = [modified_neuron_ids_dict[a_decoder_name] for a_decoder_name in decoder_names]
		assert len(individual_pf_filtered_aclus_list) == 4, f"len(individual_pf_filtered_aclus_list): {len(individual_pf_filtered_aclus_list)} but expected 4!"
		original_decoder_list = [deepcopy(pf_dict[a_decoder_name]) for a_decoder_name in decoder_names]
		## For a given run direction (LR/RL) let's require inclusion in either (OR) long v. short to be included.
		filtered_included_LR_aclus = np.union1d(individual_pf_filtered_aclus_list[0], individual_pf_filtered_aclus_list[2])
		filtered_included_RL_aclus = np.union1d(individual_pf_filtered_aclus_list[1], individual_pf_filtered_aclus_list[3])
		# build the final shared aclus:
		filtered_direction_shared_aclus_list = [filtered_included_LR_aclus, filtered_included_RL_aclus, filtered_included_LR_aclus, filtered_included_RL_aclus] # contains the shared aclus for that direction
		filtered_pf_list = [a_decoder.get_by_id(a_filtered_aclus) for a_decoder, a_filtered_aclus in zip(original_decoder_list, filtered_direction_shared_aclus_list)]
		filtered_pf_dict = dict(zip(decoder_names, filtered_pf_list))
		return filtered_pf_dict, filtered_direction_shared_aclus_list

			
	@classmethod
	def _perform_determine_pf_aclus_filtered_by_qclu_and_frate(cls, pf_dict: Dict[str, "PfND"], minimum_inclusion_fr_Hz:Optional[float]=None, included_qclu_values:Optional[List]=None):
		""" Filters the included neuron_ids by their `tuning_curve_unsmoothed_peak_firing_rates` (a property of their `.pf.ratemap`) and their `qclu` values.

		minimum_inclusion_fr_Hz: float = 5.0
		modified_long_LR_decoder = filtered_by_frate(track_templates.long_LR_decoder, minimum_inclusion_fr_Hz=minimum_inclusion_fr_Hz, debug_print=True)

		individual_decoder_filtered_aclus_list: list of four lists of aclus, not constrained to have the same aclus as its long/short pair

		Usage:
			modified_neuron_ids_dict = TrackTemplates._perform_determine_decoder_aclus_filtered_by_qclu_and_frate(pf_dict=track_templates.get_pf_dict())
			
			pf_dict=self.get_pf_dict()
			
		History: refactored from TrackTemplates._perform_determine_decoder_aclus_filtered_by_qclu_and_frate(...) on 2024-10-28 17:16 
		
			
		"""
		# original_neuron_ids_list = [a_decoder.ratemap.neuron_ids for a_decoder in (long_LR_decoder, long_RL_decoder, short_LR_decoder, short_RL_decoder)]
		original_neuron_ids_dict = {a_decoder_name:deepcopy(a_decoder.ratemap.neuron_ids) for a_decoder_name, a_decoder in pf_dict.items()}
		if (minimum_inclusion_fr_Hz is not None) and (minimum_inclusion_fr_Hz > 0.0):
			modified_neuron_ids_dict = {a_decoder_name:np.array(a_decoder.ratemap.neuron_ids)[a_decoder.ratemap.tuning_curve_unsmoothed_peak_firing_rates >= minimum_inclusion_fr_Hz] for a_decoder_name, a_decoder in pf_dict.items()}
		else:            
			modified_neuron_ids_dict = {a_decoder_name:deepcopy(a_decoder_neuron_ids) for a_decoder_name, a_decoder_neuron_ids in original_neuron_ids_dict.items()}
		
		if included_qclu_values is not None:
			# filter by included_qclu_values
			for a_decoder_name, a_decoder in pf_dict.items():
				# a_decoder.spikes_df
				neuron_identities: pd.DataFrame = deepcopy(a_decoder.filtered_spikes_df).spikes.extract_unique_neuron_identities()
				# filtered_neuron_identities: pd.DataFrame = neuron_identities[neuron_identities.neuron_type == NeuronType.PYRAMIDAL]
				filtered_neuron_identities: pd.DataFrame = deepcopy(neuron_identities)
				filtered_neuron_identities = filtered_neuron_identities[['aclu', 'shank', 'cluster', 'qclu']]
				# filtered_neuron_identities = filtered_neuron_identities[np.isin(filtered_neuron_identities.aclu, original_neuron_ids_dict[a_decoder_name])]
				filtered_neuron_identities = filtered_neuron_identities[np.isin(filtered_neuron_identities.aclu, modified_neuron_ids_dict[a_decoder_name])] # require to match to decoders
				filtered_neuron_identities = filtered_neuron_identities[np.isin(filtered_neuron_identities.qclu, included_qclu_values)] # drop [6, 7], which are said to have double fields - 80 remain
				final_included_aclus = filtered_neuron_identities['aclu'].to_numpy()
				modified_neuron_ids_dict[a_decoder_name] = deepcopy(final_included_aclus) #.tolist()
				
		return modified_neuron_ids_dict
																
											
# ==================================================================================================================== #
# Global Placefield Computation Functions                                                                              #
# ==================================================================================================================== #
""" Global Placefield perform Computation Functions """

def perform_compute_placefields(active_session_spikes_df, active_pos, computation_config: PlacefieldComputationParameters, active_epoch_placefields1D=None, active_epoch_placefields2D=None, included_epochs=None, should_force_recompute_placefields=True, progress_logger=None):
	""" Most general computation function. Computes both 1D and 2D placefields.
	active_epoch_session_Neurons:
	active_epoch_pos: a Position object
	included_epochs: a Epoch object to filter with, only included epochs are included in the PF calculations
	active_epoch_placefields1D (Pf1D, optional) & active_epoch_placefields2D (Pf2D, optional): allow you to pass already computed Pf1D and Pf2D objects from previous runs and it won't recompute them so long as should_force_recompute_placefields=False, which is useful in interactive Notebooks/scripts
	Usage:
		active_epoch_placefields1D, active_epoch_placefields2D = perform_compute_placefields(active_epoch_session_Neurons, active_epoch_pos, active_epoch_placefields1D, active_epoch_placefields2D, active_config.computation_config, should_force_recompute_placefields=True)


	NOTE: 2023-04-07 - Uses only the spikes from PYRAMIDAL cells in `active_session_spikes_df` to perform the placefield computations. 
	"""
	if progress_logger is None:
		progress_logger = lambda x, end='\n': print(x, end=end)
	## Linearized (1D) Position Placefields:
	if ((active_epoch_placefields1D is None) or should_force_recompute_placefields):
		progress_logger('Recomputing active_epoch_placefields...', end=' ')
		spikes_df = deepcopy(active_session_spikes_df).spikes.sliced_by_neuron_type('PYRAMIDAL') # Only use PYRAMIDAL neurons
		active_epoch_placefields1D = PfND.from_config_values(spikes_df, deepcopy(active_pos.linear_pos_obj), epochs=included_epochs,
										  speed_thresh=computation_config.speed_thresh, frate_thresh=computation_config.frate_thresh,
										  grid_bin=computation_config.grid_bin, grid_bin_bounds=computation_config.grid_bin_bounds, smooth=computation_config.smooth)

		progress_logger('\t done.')
	else:
		progress_logger('active_epoch_placefields1D already exists, reusing it.')

	## 2D Position Placemaps:
	if ((active_epoch_placefields2D is None) or should_force_recompute_placefields):
		progress_logger('Recomputing active_epoch_placefields2D...', end=' ')
		spikes_df = deepcopy(active_session_spikes_df).spikes.sliced_by_neuron_type('PYRAMIDAL') # Only use PYRAMIDAL neurons
		active_epoch_placefields2D = PfND.from_config_values(spikes_df, deepcopy(active_pos), epochs=included_epochs,
										  speed_thresh=computation_config.speed_thresh, frate_thresh=computation_config.frate_thresh,
										  grid_bin=computation_config.grid_bin, grid_bin_bounds=computation_config.grid_bin_bounds, smooth=computation_config.smooth)

		progress_logger('\t done.')
	else:
		progress_logger('active_epoch_placefields2D already exists, reusing it.')

	return active_epoch_placefields1D, active_epoch_placefields2D

def compute_placefields_masked_by_epochs(sess, active_config, included_epochs=None, should_display_2D_plots=False):
	""" Wrapps perform_compute_placefields to make the call simpler """
	active_session = deepcopy(sess)
	active_epoch_placefields1D, active_epoch_placefields2D = compute_placefields_as_needed(active_session, active_config.computation_config, active_config, None, None, included_epochs=included_epochs, should_force_recompute_placefields=True, should_display_2D_plots=should_display_2D_plots)
	# Focus on the 2D placefields:
	# active_epoch_placefields = active_epoch_placefields2D
	# Get the updated session using the units that have good placefields
	# active_session, active_config, good_placefield_neuronIDs = process_by_good_placefields(active_session, active_config, active_epoch_placefields)
	# debug_print_spike_counts(active_session)
	return active_epoch_placefields1D, active_epoch_placefields2D

def compute_placefields_as_needed(active_session, computation_config:PlacefieldComputationParameters=None, general_config=None, active_placefields1D = None, active_placefields2D = None, included_epochs=None, should_force_recompute_placefields=True, should_display_2D_plots=False):
	from neuropy.plotting.placemaps import plot_all_placefields

	if computation_config is None:
		computation_config = PlacefieldComputationParameters(speed_thresh=9, grid_bin=2, smooth=0.5)
	# active_placefields1D, active_placefields2D = perform_compute_placefields(active_session.neurons, active_session.position, computation_config, active_placefields1D, active_placefields2D, included_epochs=included_epochs, should_force_recompute_placefields=True)
	active_placefields1D, active_placefields2D = perform_compute_placefields(active_session.spikes_df, active_session.position, computation_config, active_placefields1D, active_placefields2D, included_epochs=included_epochs, should_force_recompute_placefields=should_force_recompute_placefields)
	# Plot the placefields computed and save them out to files:
	if should_display_2D_plots:
		ax_pf_1D, occupancy_fig, active_pf_2D_figures, active_pf_2D_gs = plot_all_placefields(active_placefields1D, active_placefields2D, general_config)
	else:
		print('skipping 2D placefield plots')
	return active_placefields1D, active_placefields2D

