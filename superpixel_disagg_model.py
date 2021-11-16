import os
os.environ["OMP_PROC_BIND"] = os.environ.get("OMP_PROC_BIND", "true")
import argparse
import pickle
import numpy as np
import torch
from sklearn.linear_model import LinearRegression
from sklearn.decomposition import PCA
import matplotlib.pyplot as plt 
from osgeo import gdal
import wandb
from pathlib import Path

import config_pop as cfg
from utils import read_input_raster_data, compute_performance_metrics, write_geolocated_image, create_map_of_valid_ids, \
    compute_grouped_values, transform_dict_to_array, transform_dict_to_matrix, calculate_densities, plot_2dmatrix, save_as_geoTIFF
from cy_utils import compute_map_with_new_labels, compute_accumulated_values_by_region, compute_disagg_weights, \
    set_value_for_each_region

from pix_transform.pix_admin_transform import PixAdminTransform
from pix_transform.pix_transform import PixTransform
from pix_transform_utils.utils import downsample,align_images
# from prox_tv import tvgen
from pix_transform_utils.plots import plot_result


def get_dataset(dataset_name, params, building_features, related_building_features):

    # configure paths
    rst_wp_regions_path = cfg.metadata[dataset_name]["rst_wp_regions_path"]
    preproc_data_path = cfg.metadata[dataset_name]["preproc_data_path"]

    # Read input data
    input_paths = cfg.input_paths[dataset_name]
    no_data_values = cfg.no_data_values[dataset_name]

    with open(preproc_data_path, 'rb') as handle:
        pdata = pickle.load(handle)

    cr_census_arr = pdata["cr_census_arr"]
    valid_ids = pdata["valid_ids"]
    no_valid_ids = pdata["no_valid_ids"]
    id_to_cr_id = pdata["id_to_cr_id"]
    fine_census = pdata["valid_census"]
    num_coarse_regions = pdata["num_coarse_regions"]
    geo_metadata = pdata["geo_metadata"]
    areas = pdata["areas"] 
    fine_regions = gdal.Open(rst_wp_regions_path).ReadAsArray().astype(np.uint32)
    wp_ids = list(np.unique(fine_regions)) 
    fine_area = dict(zip(wp_ids, areas))
    num_wp_ids = len(wp_ids)
    features = read_input_raster_data(input_paths)

    # Binary map representing a pixel belong to a region with valid id
    map_valid_ids = create_map_of_valid_ids(fine_regions, no_valid_ids)

    # Get map of coarse level regions
    cr_regions = compute_map_with_new_labels(fine_regions, id_to_cr_id, map_valid_ids)

    # Compute area of coarse regions
    cr_areas = compute_grouped_values(areas, valid_ids, id_to_cr_id)

    cr_census = {}
    for key in cr_areas.keys():
        cr_census[key] = cr_census_arr[key]

    # Reorganize features into one numpy array and handling of no-data mask
    feature_names = list(input_paths.keys())
    # torch_feature_names = torch.tensor(list(input_paths.keys()))

    # Merging building features from google and maxar if both are available
    if ('buildings_google' in feature_names) and ('buildings_maxar' in feature_names):
        # Taking the max over both available features
        #  max operation for mean building areas
        gidx = np.where([el=='buildings_google' for el in feature_names])
        midx = np.where([el=='buildings_maxar' for el in feature_names])

        maxargs = np.argmax(np.concatenate([features[gidx,:,:,None], features[midx,:,:,None]], 4), 4).astype(bool).squeeze()
 
        features[gidx,maxargs] =  features[midx,maxargs]
        feature_names[np.squeeze(gidx)] = 'buildings_merge' 
        bkeepers = np.where([el!='buildings_maxar' for el in feature_names])
        features = features[bkeepers]
        feature_names.remove('buildings_maxar') 

        if ('buildings_google_mean_area' in feature_names) and ('buildings_maxar_mean_area' in feature_names): 
            gaidx = np.where([el=='buildings_google_mean_area' for el in feature_names])
            maidx = np.where([el=='buildings_maxar_mean_area' for el in feature_names])
            
            features[gaidx,maxargs] =  features[maidx, maxargs]
            feature_names[np.squeeze(gaidx)] = 'buildings_merge_mean_area'
            bmakeepers = np.where([el!='buildings_maxar_mean_area' for el in feature_names])
            features = features[bmakeepers]
            feature_names.remove('buildings_maxar_mean_area') 
            

    # Assert that first input is a building variable
    assert(feature_names[0] in building_features)

    num_feat, ih, iw = features.shape
    valid_data_mask = torch.ones( (ih, iw), dtype=torch.bool) 
    for i, name in enumerate(feature_names):
        
        if name in (building_features + related_building_features):
            features[i][features[i]<0] = 0
        else:
            this_mask = features[i]!=no_data_values[name]
            if no_data_values[name]>1e30:
                this_mask *= ~(np.isclose(features[i],no_data_values[name]))
            valid_data_mask *= this_mask

        # Normalize the features, execpt for the buildings layer when the scale Network is used
        if (params['Net'] in ['ScaleNet']) and (name not in building_features):
            if name in list(cfg.norms[dataset_name].keys()):
                # normalize by known mean and std
                features[i] = (features[i] - cfg.norms[dataset_name][name][0]) / cfg.norms[dataset_name][name][1]
            else:
                raise Exception("Did not find precalculated mean and std")
                
    # features = torch.cat(features, 0)
    features = torch.from_numpy(features)

    # this_mask = features[0]!=no_data_values[name]
    if params["Net"]=='ScaleNet':
        valid_data_mask *= features[0]>0

    guide_res = features.shape[1:3]

    # also account for the invalid map ids
    valid_data_mask *= map_valid_ids.astype(bool)

    # Create dataformat with densities for administrative boundaries of level -1 and -2
    # Fills in the densities per pixel
    #TODO: distribute sourcemap and target map according to the building pixels! To do so, we need to calculate the number of builtup pixels per regions!
    fine_density, fine_map = calculate_densities(census=fine_census, area=fine_area, map=fine_regions)
    cr_density, cr_map = calculate_densities(census=cr_census, area=cr_areas, map=cr_regions)

    # fine_map = fine_density_map
    # cr_map = cr_density_map
    replacement = 0

    # replace -inf with 1e-16 ("-16" on log scale) is close enough to zero for the log scale, otherwise take 0
    np.nan_to_num(fine_map, copy=False, neginf=replacement)
    np.nan_to_num(cr_map, copy=False, neginf=replacement)

    cr_map = torch.from_numpy(cr_map)
    fine_map = torch.from_numpy(fine_map).float()
    valid_data_mask =  valid_data_mask.to(torch.bool)
    fine_regions = torch.from_numpy(fine_regions.astype(np.int16))
    map_valid_ids = torch.from_numpy(map_valid_ids.astype(np.bool8))
    id_to_cr_id = torch.from_numpy(id_to_cr_id.astype(np.int32))
    cr_regions = torch.from_numpy(cr_regions.astype(np.int32)) 

    # replacements of invalid values
    features[:,~valid_data_mask] = replacement
    fine_map[~valid_data_mask] = replacement
    cr_map[~valid_data_mask] = replacement
    cr_map[~valid_data_mask] = 1e-10 # TODO: verify this operation!

    dataset = {
        "features": features,
        "feature_names":feature_names,
        "cr_map": cr_map,
        "fine_map": fine_map,
        "valid_data_mask": valid_data_mask,
        "fine_regions": fine_regions,
        "map_valid_ids": map_valid_ids,
        "id_to_cr_id": id_to_cr_id,
        "cr_regions": cr_regions,
        "cr_census": cr_census,
        "fine_census": fine_census,
        "valid_ids": valid_ids,
        "guide_res": guide_res,
        "geo_metadata": geo_metadata,
        # "mean_std": (fmean, fstd),
        "num_valid_pix": valid_data_mask.sum(),
        "fine": "fine",
        "coarse": "coarse",

    }
    
    return dataset


def build_variable_list(dataset: dict, var_list: list) -> list:
    """
    Selects the variables specified in var_list from the datset and returns them as a list of same order as var_list
    """
    outlist = []
    for var in var_list:
        outlist.append(dataset[var])
    return outlist


def superpixel_with_pix_data(
    train_dataset_name,
    train_level,
    test_dataset_name,
    optimizer,
    learning_rate,
    weights_regularizer,
    weights_regularizer_adamw, 
    ):

    ####  define parameters  ########################################################

    params = {
            'weights_regularizer': weights_regularizer,#0.001, # spatial color head
            'weights_regularizer_adamw': weights_regularizer_adamw,
            'kernel_size': [1,1,1,1],
            'loss': 'NormL1',

            "admin_augment": True,
            "load_state": None, #, UGA:'fluent-star-258', TZA: 'vague-voice-185' ,'dainty-flower-151',#None, 'brisk-armadillo-86'
            "eval_only": False,
            "Net": 'ScaleNet', # Choose between ScaleNet and PixNet

            'PCA': None,

            'optim': optimizer,
            'lr': learning_rate,
            "epochs": 100,
            'logstep': 1,
            'train_dataset_name': train_dataset_name,
            'train_level': train_level,
            'test_dataset_name': test_dataset_name,
            'input_variables': list(cfg.input_paths[train_dataset_name[0]].keys())
            }

    building_features = ['buildings', 'buildings_j', 'buildings_google', 'buildings_maxar', 'buildings_merge']
    related_building_features = ['buildings_google_mean_area', 'buildings_maxar_mean_area', 'buildings_merge_mean_area']

    fine_train_source_vars = ["features", "fine_census", "fine_regions", "fine_map", "guide_res", "valid_data_mask", "fine"]
    cr_train_source_vars = ["features", "cr_census", "cr_regions", "cr_map", "guide_res", "valid_data_mask", "coarse"]
    fine_val_data_vars = ["features", "fine_census", "fine_regions", "fine_map", "valid_ids", "map_valid_ids", "guide_res", "valid_data_mask"]
    cr_disaggregation_data_vars = ["id_to_cr_id", "cr_census", "cr_regions"]

    wandb.init(project="HAC", entity="nandometzger", config=params)

    ####  load dataset  #############################################################

    assert(all(elem=="c" or elem=="f" for elem in train_level))

    train_dataset = {}
    test_dataset = {}
    train_level_dict = {}
    test_level_dict = {}
    training_source = {}
    validation_data ={}
    disaggregation_data={}
    for i,ds in enumerate(train_dataset_name):
        this_level = train_level[i]
        datapath = f"datasets/train/{ds}/{this_level}.pkl"
        if os.path.isfile(datapath):
            training_source[ds] = datapath
        else:

            train_level_dict[ds] = train_level[i]
            this_dataset = get_dataset(ds, params, building_features, related_building_features) 
            train_variables = fine_train_source_vars if train_level[i]=="f" else cr_train_source_vars
            training_source[ds] = build_variable_list(this_dataset, train_variables)

            # if ds in test_dataset_name:
        #     validation_data[ds] = build_variable_list(this_dataset, fine_val_data_vars)
        #     disaggregation_data[ds] = build_variable_list(this_dataset, cr_disaggregation_data_vars)

            # Make RAM space
            del this_dataset

    calculate_norm = False
    if calculate_norm:
        feats = torch.zeros((train_dataset[ds]["features"].shape[0],0))
        for i,ds in enumerate(train_dataset_name):
            feats = torch.cat([ feats, train_dataset[ds]["features"][:,train_dataset[ds]["valid_data_mask"]] ],1)
        print("means", feats.mean(1))
        print("stds", feats.std(1))

    for ds in test_dataset_name:
        # if ds not in list(validation_data.keys()): 
        this_level = train_level[i]
        datapath = f"datasets/test/{ds}/validationvariables.pkl"
        if os.path.isfile(datapath):
           with open(datapath, 'rb') as handle:
                validation_data[ds], disaggregation_data[ds] = pickle.load(handle)
        else:
            this_test_dataset = get_dataset(ds, params, building_features, related_building_features)
            validation_data[ds] = build_variable_list(this_test_dataset, fine_val_data_vars)
            disaggregation_data[ds] = build_variable_list(this_test_dataset, cr_disaggregation_data_vars)
            
            # Free up RAM
            del this_test_dataset
            
            Path(f"datasets/test/{ds}").mkdir(parents=True, exist_ok=True)
            with open(datapath, 'wb') as handle:
                # TODO: better save them as numpy arrays! torch get saved as huge binaries!
                pickle.dump([validation_data[ds], disaggregation_data[ds]], handle, protocol=pickle.HIGHEST_PROTOCOL)

    res = PixAdminTransform(
        training_source=training_source,
        validation_data=validation_data,
        params=params,
        disaggregation_data=disaggregation_data,
    )

    f, ax = plot_result(
        cr_map.numpy(), predicted_target_img.numpy(),
        predicted_target_img_adj.numpy(), fine_map.numpy() )
    plt.show()

    # save as geoTIFF files
    save_files = True
    if save_files:
        cr_map[~valid_data_mask]= np.nan
        predicted_target_img[~valid_data_mask]= np.nan
        predicted_target_img_adj[~valid_data_mask]= np.nan
        fine_map[~valid_data_mask]= np.nan
        dest_folder = '../../../viz/outputs/{}'.format(wandb.run.name)
        if not os.path.exists(dest_folder):
            os.makedirs(dest_folder)

        write_geolocated_image( cr_map.numpy(), dest_folder+'/source_map.tiff'.format(wandb.run.name),
            geo_metadata["geo_transform"], geo_metadata["projection"] )
        write_geolocated_image( predicted_target_img.numpy(), dest_folder+'/predicted_target_img.tiff'.format(wandb.run.name),
            geo_metadata["geo_transform"], geo_metadata["projection"] )
        write_geolocated_image( predicted_target_img_adj.numpy(), dest_folder+'/predicted_target_img_adj.tiff'.format(wandb.run.name),
            geo_metadata["geo_transform"], geo_metadata["projection"] )
        write_geolocated_image( fine_map.numpy(), dest_folder+'/validation_map.tiff'.format(wandb.run.name),
            geo_metadata["geo_transform"], geo_metadata["projection"] )
        write_geolocated_image( scales.numpy(), dest_folder+'/scales.tiff'.format(wandb.run.name),
            geo_metadata["geo_transform"], geo_metadata["projection"] )

    return


def main():
    parser = argparse.ArgumentParser()
    # parser.add_argument("preproc_data_path", type=str, help="Preprocessed data of regions (pickle file)")
    # parser.add_argument("rst_wp_regions_path", type=str,
                        # help="Raster of WorldPop administrative boundaries information") 
    parser.add_argument("--train_dataset_name", "-train", nargs='+', help="Train Dataset name (separated by commas)", required=True)
    parser.add_argument("--train_level", "-train_lvl", nargs='+', help="ordered by --train_dataset_name [f:finest, c: coarser level] (separated by commas) ", required=True)
    parser.add_argument("--test_dataset_name", "-test", nargs='+', help="Test Dataset name (separated by commas)", required=True)
    parser.add_argument("--optimizer", "-optim", type=str, default="adamw", help=" ")
    parser.add_argument("--learning_rate", "-lr", type=float, default=0.00001, help=" ")
    parser.add_argument("--weights_regularizer", "-wr", type=float, default=0., help=" ")
    parser.add_argument("--weights_regularizer_adamw", "-adamwr", type=float, default=0.001, help=" ")
    args = parser.parse_args()

    args.train_dataset_name = args.train_dataset_name[0].split(",")
    args.train_level = args.train_level[0].split(",")
    args.test_dataset_name = args.test_dataset_name[0].split(",")

    superpixel_with_pix_data( 
        args.train_dataset_name,
        args.train_level,
        args.test_dataset_name,
        args.optimizer,
        args.learning_rate,
        args.weights_regularizer,
        args.weights_regularizer_adamw, 
    )


if __name__ == "__main__":
    main()
