import fiona
from osgeo import gdal
import numpy as np
from sklearn.metrics import r2_score, mean_absolute_error, mean_squared_error
from sklearn.utils import check_array
from sklearn.model_selection import KFold
from tqdm import tqdm
import copy
from pylab import figure, imshow, matshow, grid, savefig
import torch
import pickle
import h5py
import wandb
import psutil
import os
import pdb

def get_properties_dict(data_dict_orig):
    data_dict = []
    for data_row in data_dict_orig:
        data_dict.append(data_row["properties"])
    return data_dict


def read_input_raster_data_to_np(input_paths):
    #assuming every covariate has same dimensions
    first_name = list(input_paths.keys())[0]
    hwdims = gdal.Open(input_paths[first_name]).ReadAsArray().astype(np.float32).shape
    fdim = input_paths.__len__()
    inputs = np.zeros((fdim,) + hwdims, dtype=np.float32) 
    for i,kinp in enumerate(input_paths.keys()):
        print("read {}".format(input_paths[kinp]))
        inputs[i] = gdal.Open(input_paths[kinp]).ReadAsArray().astype(np.float32)
    return inputs


def read_input_raster_data(input_paths):
    inputs = {}
    for kinp in input_paths.keys():
        print("read {}".format(input_paths[kinp]))
        inputs[kinp] = gdal.Open(input_paths[kinp]).ReadAsArray().astype(np.float32)
    return inputs


def read_shape_layer_data(shape_layer_path):
    with fiona.open(shape_layer_path) as reader:
        layer_data_orig = [elem for elem in reader]
    layer_data = get_properties_dict(layer_data_orig)
    return layer_data

def mean_absolute_percentage_error(y_true, y_pred): 

    y_true = check_array(y_true.reshape(-1,1))
    y_pred = check_array(y_pred.reshape(-1,1))
    
    zeromask = (y_true!=0)
    y_true, y_pred = y_true[zeromask], y_pred[zeromask]  

    percentage_error = (y_true - y_pred) / y_true

    return np.mean(np.abs(percentage_error)) * 100, percentage_error * 100


def my_mean_absolute_error(y_pred,y_true):
    errors = y_pred - y_true
    output_errors = np.average(np.abs(errors), axis=0)
    return output_errors, errors


def compute_performance_metrics_arrays(preds, gt): 
    
    metrics = {}

    preds = np.squeeze(preds)
    gt = np.squeeze(gt)

    if len(preds.shape)==2:
        # bayes
        # preds, vars = np.split(preds,[1], axis=1)
        stds = np.sqrt(preds[:,1])
        preds = preds[:,0]

        metrics.update({
            "aux/stds/histogram_std": stds, "aux/stds/min_stds": np.min(stds),
            "aux/stds/max_stds":  np.max(stds), "aux/stds/median_stds": np.median(stds),
            "aux/stds/mean_stds":  np.mean(stds), "aux/stds/std_stds": np.std(stds)
        })
    
    r2 = r2_score(gt, preds)
    
    mae, errors = my_mean_absolute_error(gt, preds)
    mse = mean_squared_error(gt, preds)
    mape, percentage_error = mean_absolute_percentage_error(gt,preds)

    metrics.update({
    "r2": r2, "mae": mae, "mse": mse, "mape": mape,
    "aux/errors/errors": errors, "aux/errors/min_errors": np.min(errors), "aux/errors/max_errors":  np.max(errors), "aux/errors/median_error":  np.median(errors), "aux/errors/mean_error":  np.mean(errors), "aux/errors/std_error":  np.std(errors), 
    # "aux/errors/abs/abs_errors": np.abs(errors), "aux/errors/abs/min_abs_errors": np.min(np.abs(errors)), "aux/errors/abs/max_abs_error":  np.max(np.abs(errors)), "aux/errors/abs/median_abs_error":  np.median(np.abs(errors)), "aux/errors/abs/mean_abs_error": np.mean(np.abs(errors)), "aux/errors/abs/std_abs_error": np.std(np.abs(errors)),
    "aux/errors_percentage/percentage_errors": percentage_error, "aux/errors_percentage/min_percentage_errors": np.min(percentage_error), "aux/errors_percentage/max_percentage_error":  np.max(percentage_error), "aux/errors_percentage/median_percentage_error":  np.median(percentage_error), "aux/errors_percentage/mean_percentage_error":  np.mean(percentage_error), "aux/errors_percentage/std_percentage_error": np.std(percentage_error),
    # "aux/errors_percentage/abs/abs_percentage_errors": np.abs(percentage_error), "aux/errors_percentage/abs/min_abs_percentage_errors": np.min(np.abs(percentage_error)), "aux/errors_percentage/abs/max_abs_percentage_errors":  np.max(np.abs(percentage_error)), "aux/errors_percentage/abs/median_abs_percentage_error":  np.median(np.abs(percentage_error)), "aux/errors_percentage/abs/mean_abs_percentage_error":  np.mean(np.abs(percentage_error)), "aux/errors_percentage/abs/std_abs_percentage_error":  np.std(np.abs(percentage_error))
    })

    return metrics


def compute_performance_metrics(preds_dict, gt_dict):
    assert len(preds_dict) == len(gt_dict)

    preds = []
    gt = []
    ids = preds_dict.keys()
    for id in ids:
        preds.append(preds_dict[id])
        gt.append(gt_dict[id])

    preds = np.array(preds).astype(np.float)
    gt = np.array(gt).astype(np.float)

    return compute_performance_metrics_arrays(preds, gt)


def write_geolocated_image(image, output_path, src_geo_transform, src_projection):
    driver = gdal.GetDriverByName("GTiff")
    outdata = driver.Create(output_path, image.shape[1], image.shape[0], 1, gdal.GDT_Float32, options=['COMPRESS=LZW'])
    outdata.SetGeoTransform(src_geo_transform)
    outdata.SetProjection(src_projection)
    outdata.GetRasterBand(1).WriteArray(image)
    outdata.FlushCache()
    outdata = None
    ds = None


def convert_str_to_int_keys(data_dict_orig):
    data_dict = {}
    for k in data_dict_orig.keys():
        data_dict[int(k)] = data_dict_orig[k]
    return data_dict


def convert_dict_vals_str_to_float(data_dict_orig):
    return {k: float(data_dict_orig[k]) for k in data_dict_orig.keys()}


def preprocess_census_targets(data_dict_orig):
    data_dict = convert_str_to_int_keys(data_dict_orig)
    data_dict = convert_dict_vals_str_to_float(data_dict)
    return data_dict


def create_map_of_valid_ids(regions, no_valid_ids):
    map_valid_ids = np.ones(regions.shape).astype(np.uint32)
    for id in no_valid_ids:
        map_valid_ids[regions == id] = 0
    return map_valid_ids


def create_valid_mask_array(num_ids, valid_ids):
    valid_ids_mask = np.zeros(num_ids)
    for id in valid_ids:
        valid_ids_mask[id] = 1
    return valid_ids_mask


def compute_grouped_values(data, valid_ids, id_to_gid):
    # Initialize target values
    grouped_data = {}
    for id in valid_ids:
        gid = id_to_gid[id]
        if gid not in grouped_data.keys():
            grouped_data[gid] = 0
    # Aggregate targets
    for id in valid_ids:
        gid = id_to_gid[id]
        grouped_data[gid] += data[id]
    return grouped_data


def transform_dict_to_array(data_dict):
    return np.array([data_dict[k] for k in data_dict.keys()]).astype(np.float32)


def transform_dict_to_matrix(data_dict):
    assert len(data_dict.keys()) > 0
    # get size of matrix
    keys = list(data_dict.keys())
    num_rows = len(keys)
    first_row = data_dict[keys[0]]
    col_keys = list(first_row.keys())
    num_cols = len(col_keys)
    # fill matrix
    data_array = np.zeros((num_rows, num_cols)).astype(np.float32)
    for i, rk in enumerate(keys):
        for j, ck in enumerate(col_keys):
            data_array[i, j] = data_dict[rk][ck]

    return data_array


def compute_features_from_raw_inputs(inputs, feats_list):
    inputs_mat = []
    for feat in feats_list:
        inputs_mat.append(inputs[feat])
    inputs_mat = np.array(inputs_mat)
    all_features = inputs_mat.reshape((inputs_mat.shape[0], -1))
    all_features = all_features.transpose()
    return all_features


def mostly_non_empty_map(map_valid_ids, feats_list, inputs, threshold = 0.99, min_val = 0.001):
    map_empty_feats = np.random.rand(map_valid_ids.shape[0], map_valid_ids.shape[1]) < threshold
    for k in feats_list:
        min_threshold = 0
        max_threshold = 1000.0
        for k in inputs.keys():
            inputs[k][inputs[k] > max_threshold] = 0
            inputs[k][inputs[k] < min_threshold] = 0
        map_empty_feats = np.multiply(map_empty_feats, inputs[k] <= min_val)

    mostly_non_empty = (1 - map_empty_feats).astype(np.bool)
    return mostly_non_empty


def calculate_densities(census, area, map=None):
    density = {}
    for key, value in census.items():
        density[key] = value / area[key]
    if map is None:
        return density

    #write into map
    # making sure that all the values are contained in the 
    diffkey = set(area.keys()) - set(census.keys())
    mapping = copy.deepcopy(density)
    for key in diffkey:
        mapping[key] = 0

    #vectorized mapping of the integer keys (assumes keys are integers, and not excessively large compared to the length of the dicct)
    k = np.array(list(mapping.keys()))
    v = np.array(list(mapping.values()))
    mapping_ar = np.zeros(k.max()+1,dtype=v.dtype) #k,v from approach #1
    mapping_ar[k] = v
    density_map = mapping_ar[map] 
    return density, density_map
    
    
def plot_2dmatrix(matrix,fig=1):
    if torch.is_tensor(matrix):
        if matrix.is_cuda:
            matrix = matrix.cpu()
        matrix = matrix.numpy()
    figure(fig)
    matshow(matrix, interpolation='nearest')
    grid(True)
    savefig('outputs/last_plot.png')


def accumulate_values_by_region(map, ids, regions):
    sums = {}
    for id in tqdm(ids):
        sums[id]= map[regions==id].sum()
    return sums


def bbox2(img):
    rows = torch.any(img, axis=1)
    cols = torch.any(img, axis=0)
    rmin, rmax = torch.where(rows)[0][[0, -1]]
    cmin, cmax = torch.where(cols)[0][[0, -1]]

    return rmin, rmax, cmin, cmax


class PatchDataset(torch.utils.data.Dataset):
    """Patch dataset."""
    def __init__(self, rawsets, memory_mode, device, validation_split): 
        self.device = device
        
        print("Preparing dataloader for: ", list(rawsets.keys()))
        self.loc_list = []
        self.BBox = {}
        self.features = {}
        self.Ys = {}
        self.Masks = {}
        for i, (name, rs)  in tqdm(enumerate(rawsets.items())):

            with open(rs['vars'], "rb") as f: 
                tr_census, tr_regions, tr_valid_data_mask, tY, tMasks, tBBox = pickle.load(f)

            self.BBox[name] = tBBox
            if memory_mode:
                self.features[name] = h5py.File(rs["features"], 'r')["features"][:]
            else:
                self.features[name] = h5py.File(rs["features"], 'r')["features"]
            self.Ys[name] =  tY  
            self.Masks[name] = tMasks
            self.loc_list.extend( [(name, k) for k,_ in enumerate(tBBox)])

        self.dims = self.features[name].shape[1]
        
    def __len__(self):
        return len(self.variables[0])

    def getsingleitem(self, idx):
        output = []
        name, k = self.idx_to_loc(idx)
        rmin, rmax, cmin, cmax = self.BBox[name][k]
        X = torch.from_numpy(self.features[name][:,:,rmin:rmax, cmin:cmax])
        Y = torch.from_numpy(self.Ys[name][k]) 
        Mask = torch.from_numpy(self.Masks[name][k]) 
        return X, Y, Mask

    def __getitem__(self, idx):
        return self.getsingleitem(idx)

class MultiPatchDataset(torch.utils.data.Dataset):
    """Patch dataset."""
    def __init__(self, datalocations, train_dataset_name, train_level, memory_mode, device,
        validation_split, validation_fold, loss_weights, sampler_weights, val_valid_ids={}, build_pairs=True):

        self.device = device
        
        print("Preparing dataloader for: ", list(datalocations.keys()))
        self.features = {}
        self.loc_list, self.loc_list_train, self.loc_list_val = [],[],[]
        self.all_weights, self.all_sampler_weights,  self.all_natural_weights = [],[],[]
        self.BBox, self.BBox_train, self.BBox_val = {},{},{}
        self.Ys, self.Ys_train, self.Ys_val = {},{},{} 
        self.tregid, self.max_tregid = {},{}
        self.tregid_val, self.max_tregid_val = {},{}
        self.Masks, self.Masks_train, self.Masks_val = {},{},{}
        self.weight_list = {}
        self.memory_disag, self.memory_disag_val, self.feature_names = {},{},{}
        self.val_valid_ids = val_valid_ids
        self.memory_vars = {}
        self.source_census_val = {}
        process = psutil.Process(os.getpid())
        for i, (name, rs) in tqdm(enumerate(datalocations.items())):
            print("Preparing dataloader: ", name)
            print("Initial:",process.memory_info().rss/1000/1000,"mb used")

            with open(rs['train_vars_f'], "rb") as f:
                _, _, _, tY_f, tregid_f, tMasks_f, tBBox_f, _ = pickle.load(f)
            with open(rs['train_vars_c'], "rb") as f:
                _, _, _, tY_c, tregid_c, tMasks_c, tBBox_c, feature_names = pickle.load(f)

            self.feature_names[name] = feature_names
            # print("After loading trainvars",process.memory_info().rss/1000/1000,"mb used")

            if name not in self.val_valid_ids.keys():          
                with open(rs['eval_vars'], "rb") as f:
                    self.memory_vars[name] = pickle.load(f)
                    self.val_valid_ids[name] = self.memory_vars[name][3]
            # print("After loading of eval memory vars",process.memory_info().rss/1000/1000,"mb used")
            with open(rs['disag'], "rb") as f:
                self.memory_disag[name] = pickle.load(f) 

            # print("After loading of disag memory",process.memory_info().rss/1000/1000,"mb used")

            if memory_mode[i]=='m':
                #self.features[name] = h5py.File(rs["features"], 'r', driver='core')["features"]
                self.features[name] = h5py.File(rs["features"], 'r')["features"][:]

            elif memory_mode[i]=='d':
                self.features[name] = h5py.File(rs["features"], 'r')["features"]
            else:
                raise Exception(f"Wrong memory mode for {name}. It should be 'd' or 'm' in a comma separated list. No spaces!")
            # print("After loading of features",process.memory_info().rss/1000/1000,"mb used")
            
            # Validation split strategy:
            # We always split the coarse patches into 5 folds, then we look up fine patches that belong to those coarse validation patches
            np.random.seed(1610)
            if validation_fold is not None:
                kf = KFold(n_splits=5, shuffle=True, random_state=1610)
                trainidxs, validxs = [],[]
                for train_index, val_index in kf.split(tY_c):
                    trainidxs.append(train_index)
                    validxs.append(val_index)
                choice_val_c = validxs[validation_fold]
            else:
                split_int =int(len(tY_c)*validation_split)
                choice_val_c = np.random.choice(range(len(tY_c)), size=(split_int,), replace=False)   
            ind_val_c = np.zeros(len(tY_c), dtype=bool)
            ind_val_c[choice_val_c] = True 
            ind_train_c = ~ind_val_c

            tY_f = np.asarray(tY_f)
            tMasks_f = np.asarray(tMasks_f, dtype=object)
            tBBox_f = np.asarray(tBBox_f)
            tregid_f = np.asarray(tregid_f).astype(np.int16)
            tregid_c = np.asarray(tregid_c).astype(np.int16)

            tregid_val_c = tregid_c[choice_val_c]

            # Prepare validation variables
            # If we took the coarse level as training, we need to translate the ind_val to the fine level and get the fine level patches for validation!
            choice_val_f = np.where(np.in1d(self.memory_disag[name][0],tregid_val_c)[self.val_valid_ids[name]])[0] 
            ind_val_f = np.zeros(len(tY_f), dtype=bool)
            ind_val_f[choice_val_f] = True 
            ind_train_f = ~ind_val_f

            if train_level[i]=='f':
                tY, tregid, tMasks, tBBox = tY_f, tregid_f, tMasks_f, tBBox_f
                ind_train = ind_train_f
            elif train_level[i]=='c':
                tY, tregid, tMasks, tBBox = tY_c, tregid_c, tMasks_c, tBBox_c
                ind_train = ind_train_c

            tY = np.asarray(tY)
            tMasks = np.asarray(tMasks, dtype=object)
            tBBox = np.asarray(tBBox)

            self.BBox_val[name] = tBBox_f[ind_val_f]
            valid_val_boxes = (self.BBox_val[name][:,1]-self.BBox_val[name][:,0]) * (self.BBox_val[name][:,3]-self.BBox_val[name][:,2])>0
            self.BBox_val[name] = self.BBox_val[name][valid_val_boxes]
            self.Ys_val[name] =  tY_f[ind_val_f][valid_val_boxes] 
            self.tregid_val[name] = tregid_f[ind_val_f][valid_val_boxes]
            target_to_source_val = self.memory_disag[name][0].clone()
            target_to_source_val[~np.in1d(self.memory_disag[name][0], tregid_val_c)] = 0
            # coarse_regid_val = self.memory_disag[name][0][self.tregid_val[name]].unique(return_counts=True)[0] # consistency check: this should be the same as "tregid_val_c"
            self.source_census_val[name] = { key: value for key,value in self.memory_disag[name][1].items() if key in tregid_val_c}
            self.memory_disag_val[name] = target_to_source_val, self.source_census_val[name], self.memory_disag[name][2]
            self.max_tregid_val[name] = np.max(self.tregid_val[name])
            self.Masks_val[name] = tMasks_f[ind_val_f][valid_val_boxes]
            self.loc_list_val.extend( [(name, k) for k,_ in enumerate(self.BBox_val[name])])

            # Prepare the training variables
            self.BBox_train[name] = tBBox[ind_train]
            valid_train_boxes = (self.BBox_train[name][:,1]-self.BBox_train[name][:,0]) * (self.BBox_train[name][:,3]-self.BBox_train[name][:,2])>0
            self.BBox_train[name] = self.BBox_train[name][valid_train_boxes] 
            self.Ys_train[name] =  tY[ind_train][valid_train_boxes]
            self.Masks_train[name] = tMasks[ind_train][valid_train_boxes]
            if name in train_dataset_name:
                self.loc_list_train.extend( [(name, k) for k,_ in enumerate(self.BBox_train[name])])

            # Prepare the complete variables, we only use the finest level for this
            self.BBox[name] = tBBox_f
            valid_boxes = (self.BBox[name][:,1]-self.BBox[name][:,0]) * (self.BBox[name][:,3]-self.BBox[name][:,2])>0
            self.BBox[name] = self.BBox[name][valid_boxes] 
            self.Ys[name] =  tY_f[valid_boxes]
            self.tregid[name] = tregid_f[valid_boxes]
            self.max_tregid[name] = np.max(self.tregid[name])
            self.Masks[name] = tMasks_f[valid_boxes]
            self.loc_list.extend( [(name, k) for k,_ in enumerate(self.BBox[name])])

            # Initialize sample weights
            self.weight_list[name] =  torch.tensor([loss_weights[i]]*len(self.Ys_train[name]), requires_grad=False)
            self.all_weights.extend(self.weight_list[name])
            self.all_sampler_weights.extend( [sampler_weights[i]] * len(self.Ys_train[name]) )
            self.all_natural_weights.extend([len(self.Ys_train[name])] * len(self.Ys_train[name]))
            print("Final usage",process.memory_info().rss/1000/1000,"mb used")

        self.dims = self.features[name].shape[1]

        if build_pairs:  
            
            num_single = len(self.loc_list_train)
            indicies = range(num_single)
            max_pix_forward = 20000

            bboxlist = [ self.BBox[name][k] for name,k in self.loc_list_train ]
            patchsize = [ (bb[1]-bb[0])*(bb[3]-bb[2]) for bb in bboxlist]
            patchsize = np.asarray(patchsize)

            pairs = [[indicies[i],indicies[j]] for i in range(num_single) for j in range(i+1, num_single)]
            pairs = np.asarray(pairs) 
            sumpixels_pairs12 = np.take(patchsize, pairs[:,0]) + np.take(patchsize, pairs[:,1])  
            pairs = pairs[np.asarray(sumpixels_pairs12)<max_pix_forward**2]
            self.small_pairs = pairs[np.asarray(sumpixels_pairs12)>0]

            # triplets = [[indicies[i],indicies[j],indicies[k]] for i in tqdm(range(num_single)) for j in range(i+1, num_single) for k in range(j+1, num_single)]
            # triplets = np.asarray(triplets, dtype=object)
            # sumpixels_triplets = [(patchsize[id1]+patchsize[id2]+patchsize[id3]) for id1,id2,id3 in triplets ]
            # self.small_triplets = triplets[np.asarray(sumpixels_triplets)<max_pix_forward**2]

            # prepare the weights
            self.all_sample_ids = list(self.small_pairs) #+ list(self.small_triplets)
            self.custom_sampler_weights = [ self.all_sampler_weights[idx1]+self.all_sampler_weights[idx2] for idx1,idx2 in self.all_sample_ids ]
            self.natural_sampler_weights = [ self.all_natural_weights[idx1]+self.all_natural_weights[idx2] for idx1,idx2 in self.all_sample_ids ]

        print("Dataloader ready.")

    def __len__(self):
        # this will return the length when the data is used for training with a dataloader
        return self.all_sample_ids.__len__()
    
    def len_val(self):
        # this will return the length of the validation dataset
        return len(self.loc_list_val)
    
    def len_all_samples(self, name=None):
        # length when we merge training and validation together
        if name is not None:
            return len(self.Ys[name])
        return len(self.loc_list)

    def idx_to_loc(self, idx):
        return self.loc_list[idx]

    def idx_to_loc_train(self, idx):
        return self.loc_list_train[idx]

    def idx_to_loc_val(self, idx):
        return self.loc_list_val[idx]
    
    def num_feats(self):
        return self.dims

    def get_single_item(self, idx, name=None): 
        if name is None:
            name, k = self.idx_to_loc_val(idx)
        else:
            k = idx 
        rmin, rmax, cmin, cmax = self.BBox[name][k]
        X = torch.tensor(self.features[name][0,:,rmin:rmax, cmin:cmax])
        Y = torch.tensor(self.Ys[name][k])
        Mask = torch.tensor(self.Masks[name][k]) 
        census_id = torch.tensor(self.tregid[name][k])
        return X, Y, Mask, name, census_id


    def get_single_training_item(self, idx, name=None): 
        if name is None:
            name, k = self.idx_to_loc_train(idx)
        else:
            k = idx
        rmin, rmax, cmin, cmax = self.BBox_train[name][k]
        X = torch.tensor(self.features[name][0,:,rmin:rmax, cmin:cmax])
        Y = torch.tensor(self.Ys_train[name][k])
        Mask = torch.tensor(self.Masks_train[name][k])
        weight = self.weight_list[name][k]
        return X, Y, Mask, name, weight

    def get_single_validation_item(self, idx, name=None, return_BB=False): 
        if name is None:
            name, k = self.idx_to_loc_val(idx)
        else:
            k = idx
        rmin, rmax, cmin, cmax = self.BBox_val[name][k]
        X = torch.tensor(self.features[name][0,:,rmin:rmax, cmin:cmax])
        Y = torch.tensor(self.Ys_val[name][k])
        Mask = torch.tensor(self.Masks_val[name][k])
        census_id = torch.tensor(self.tregid_val[name][k])
        if np.prod(X.shape[1:])==0:
            raise Exception("no values")
        if return_BB:
            return X, Y, Mask, name, census_id, self.BBox_val[name][k]
        else:
            return X, Y, Mask, name, census_id

    def __getitem__(self,idx):
        idxs = self.all_sample_ids[idx] 
        sample = []
        for i in idxs:
            sample.append(self.get_single_training_item(i))
        
        return sample


def NormL1(outputs, targets, eps=1e-8):
    loss = torch.abs(outputs - targets) / torch.clip(outputs + targets, min=eps)
    return loss.mean()

def LogL1(outputs, targets, eps=1e-8):
    loss = torch.abs(torch.log(outputs+1) - torch.log(targets+1))
    return loss.mean()

def LogoutputL1(outputs, targets, eps=1e-8):
    loss = torch.abs(outputs - torch.log(targets))
    return loss.mean()

def LogoutputL2(outputs, targets, eps=1e-8):
    loss = (outputs - torch.log(targets+1))**2
    return loss.mean()

def save_as_geoTIFF(src_filename,  dst_filename, raster): 
 
    from osgeo import gdal, osr

    src_filename ='/path/to/source.tif'
    dst_filename = '/path/to/destination.tif'

    # Opens source dataset
    src_ds = gdal.Open(src_filename)
    format = "GTiff"
    driver = gdal.GetDriverByName(format)

    # Open destination dataset
    dst_ds = driver.CreateCopy(dst_filename, src_ds, 0)

    # Specify raster location through geotransform array
    # (uperleftx, scalex, skewx, uperlefty, skewy, scaley)
    # Scale = size of one pixel in units of raster projection
    # this example below assumes 100x100
    gt = [-7916400, 100, 0, 5210940, 0, -100]

    # Set location
    dst_ds.SetGeoTransform(gt)

    # Get raster projection
    epsg = 3857
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    dest_wkt = srs.ExportToWkt()

    # Set projection
    dst_ds.SetProjection(dest_wkt)

    # Close files
    dst_ds = None
    src_ds = None



    # src_path = '/home/pf/pfstaff/projects/andresro/typhoon/baselines'
    # src_path_profiles = '/scratch/for_andres/before/orig'

    # algs = ['cva-2', 'mad-2']
    # tiles = ['T51PUR', 'T51PUT', 'T51PVR', 'T51PWQ']
    
    # for alg in algs:
    #     for tile in tiles:
    #         print(f'Processing {alg}/{tile}...')
    #         mat_fname = os.path.join(src_path, alg, f'{tile}.mat')
    #         mat_file = sio.loadmat(mat_fname)
    #         cm = mat_file['CM'].astype('float32')
            
    #         profile_src_path = os.path.join(src_path_profiles, f'{tile}_mean_12.tif')
    #         profile_src = rasterio.open(profile_src_path)
    #         profile = profile_src.profile.copy()
    #         profile['count'] = 1
    #         profile_src.close()
            
    #         out_path = os.path.join(alg, f'{tile}_mean_12.tif')
    #         writer = rasterio.open(out_path, 'w', **profile)
            
    #         writer.write(cm, indexes=1)
    #         writer.close()
            
    # print('End')
