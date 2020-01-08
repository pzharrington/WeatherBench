from src.score import *
import os
import ast, re
import numpy as np
import xarray as xr
import tensorflow as tf
import tensorflow.keras as keras
from tensorflow.keras.layers import Input, Dropout, Conv2D, Lambda
import tensorflow.keras.backend as K
from configargparse import ArgParser
import datetime

def limit_mem():
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    tf.Session(config=config)


class DataGenerator(keras.utils.Sequence):
    def __init__(self, ds, var_dict, lead_time, batch_size=32, shuffle=True, load=True,
                 mean=None, std=None, output_vars=None):
        """
        Data generator for WeatherBench data.
        Template from https://stanford.edu/~shervine/blog/keras-how-to-generate-data-on-the-fly
        Args:
            ds: Dataset containing all variables
            var_dict: Dictionary of the form {'var': level}. Use None for level if data is of single level
            lead_time: Lead time in hours
            batch_size: Batch size
            shuffle: bool. If True, data is shuffled.
            load: bool. If True, datadet is loaded into RAM.
            mean: If None, compute mean from data.
            std: If None, compute standard deviation from data.
        """
        print('DG start', datetime.datetime.now().time())
        self.ds = ds
        self.var_dict = var_dict
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.lead_time = lead_time

        data = []
        level_names = []
        generic_level = xr.DataArray([1], coords={'level': [1]}, dims=['level'])
        for long_var, params in var_dict.items():
            if long_var == 'constants':
                for var in params:
                    data.append(ds[var].expand_dims(
                        {'level': generic_level, 'time': ds.time}, (1, 0)
                    ))
                    level_names.append(var)
            else:
                var, levels = params
                try:
                    data.append(ds[var].sel(level=levels))
                    level_names += [f'{var}_{level}' for level in levels]
                except ValueError:
                    data.append(ds[var].expand_dims({'level': generic_level}, 1))
                    level_names.append(var)

        self.data = xr.concat(data, 'level').transpose('time', 'lat', 'lon', 'level')
        self.data['level_names'] = xr.DataArray(
            level_names, dims=['level'], coords={'level': self.data.level})
        if output_vars is None:
            self.output_idxs = range(len(self.data.level))
        else:
            self.output_idxs = [i for i, l in enumerate(self.data.level_names.values)
                                if any([bool(re.match(o, l)) for o in output_vars])]

        # Normalize
        print('DG normalize', datetime.datetime.now().time())
        self.mean = self.data.mean(('time', 'lat', 'lon')).compute() if mean is None else mean
        #         self.std = self.data.std('time').mean(('lat', 'lon')).compute() if std is None else std
        self.std = self.data.std(('time', 'lat', 'lon')).compute() if std is None else std
        self.data = (self.data - self.mean) / self.std

        self.n_samples = self.data.isel(time=slice(0, -lead_time)).shape[0]
        self.init_time = self.data.isel(time=slice(None, -lead_time)).time
        self.valid_time = self.data.isel(time=slice(lead_time, None)).time

        self.on_epoch_end()

        # For some weird reason calling .load() earlier messes up the mean and std computations
        print('DG load', datetime.datetime.now().time())
        if load: print('Loading data into RAM'); self.data.load()
        print('DG done', datetime.datetime.now().time())

    def __len__(self):
        'Denotes the number of batches per epoch'
        return int(np.ceil(self.n_samples / self.batch_size))

    def __getitem__(self, i):
        'Generate one batch of data'
        idxs = self.idxs[i * self.batch_size:(i + 1) * self.batch_size]
        X = self.data.isel(time=idxs).values
        y = self.data.isel(time=idxs + self.lead_time, level=self.output_idxs).values
        return X, y

    def on_epoch_end(self):
        'Updates indexes after each epoch'
        self.idxs = np.arange(self.n_samples)
        if self.shuffle == True:
            np.random.shuffle(self.idxs)

class PeriodicConv2D(tf.keras.layers.Conv2D):
    """Convolution with periodic padding in second spatial dimension (lon)"""
    def __init__(self, filters, kernel_size, **kwargs):
        assert type(kernel_size) is int, 'Periodic convolutions only works for square kernels.'
        self.pad_width = (kernel_size - 1) // 2
        super().__init__(filters, kernel_size, **kwargs)
        assert self.padding == 'valid', 'Periodic convolution only works for valid padding.'
        assert sum(self.strides) == 2, 'Periodic padding only works for stride (1, 1)'
    
    def _pad(self, inputs):
        # Input: [samples, lat, lon, filters]
        # Periodic padding in lon direction
        inputs_padded = tf.concat(
            [inputs[:, :, -self.pad_width:, :], inputs, inputs[:, :, :self.pad_width, :]], axis=2)
        # Zero padding in the lat direction
        inputs_padded = tf.pad(inputs_padded, [[0, 0], [self.pad_width, self.pad_width], [0, 0], [0, 0]])
        return inputs_padded

    def __call__(self, inputs, *args, **kwargs):
        # Unfortunate workaround necessary for TF < 1.13
        inputs_padded = Lambda(self._pad)(inputs)
        return super().__call__(inputs_padded, *args, **kwargs)


def build_cnn(filters, kernels, input_shape, activation='elu', dr=0):
    """Fully convolutional network"""
    x = input = Input(shape=input_shape)
    for f, k in zip(filters[:-1], kernels[:-1]):
        x = PeriodicConv2D(f, k, activation=activation)(x)
        if dr > 0: x = Dropout(dr)(x)
    output = PeriodicConv2D(filters[-1], kernels[-1])(x)
    return keras.models.Model(input, output)


def create_predictions(model, dg):
    """Create non-iterative predictions"""
    preds = xr.DataArray(
        model.predict_generator(dg),
        dims=['time', 'lat', 'lon', 'level'],
        coords={'time': dg.valid_time, 'lat': dg.data.lat, 'lon': dg.data.lon,
                'level': dg.data.isel(level=dg.output_idxs).level,
                'level_names': dg.data.isel(level=dg.output_idxs).level_names
                },
    )
    # Unnormalize
    preds = (preds * dg.std.isel(level=dg.output_idxs).values +
             dg.mean.isel(level=dg.output_idxs).values)

    das = []
    for long_var, params in dg.var_dict.items():
        if not long_var == 'constants':
            var, levels = params
            var_names = [var] if levels is None else [f'{var}_{level}' for level in levels]
            var_idxs = [i for i, v in enumerate(preds.level_names) if v in var_names]
            da = preds.isel(level=var_idxs)
            if levels is not None:
                da = da.sel(level=levels).drop('level_names')
            else:
                da = da.squeeze().drop('level').drop('level_names')
            das.append({var: da})
    return xr.merge(das)

# TODO: Outdated
# def create_iterative_predictions(model, dg, max_lead_time=5 * 24):
#     """Create iterative predictions"""
#     state = dg.data[:dg.n_samples]
#     preds = []
#     for _ in range(max_lead_time // dg.lead_time):
#         state = model.predict(state)
#         p = state * dg.std.values + dg.mean.values
#         preds.append(p)
#     preds = np.array(preds)
#
#     lead_time = np.arange(dg.lead_time, max_lead_time + dg.lead_time, dg.lead_time)
#     das = []
#     lev_idx = 0
#     for var, levels in dg.var_dict.items():
#         if levels is None:
#             das.append(xr.DataArray(
#                 preds[:, :, :, :, lev_idx],
#                 dims=['lead_time', 'time', 'lat', 'lon'],
#                 coords={'lead_time': lead_time, 'time': dg.init_time, 'lat': dg.ds.lat, 'lon': dg.ds.lon},
#                 name=var
#             ))
#             lev_idx += 1
#         else:
#             nlevs = len(levels)
#             das.append(xr.DataArray(
#                 preds[:, :, :, :, lev_idx:lev_idx + nlevs],
#                 dims=['lead_time', 'time', 'lat', 'lon', 'level'],
#                 coords={'lead_time': lead_time, 'time': dg.init_time, 'lat': dg.ds.lat, 'lon': dg.ds.lon,
#                         'level': levels},
#                 name=var
#             ))
#             lev_idx += nlevs
#     return xr.merge(das)

def create_cnn(filters, kernels, dropout=0., activation='elu', periodic=True):
    assert len(filters) == len(kernels), 'Requires same number of filters and kernel_sizes.'
    input = Input(shape=(None, None, 1,))
    x = input
    for f, k in zip(filters[:-1], kernels[:-1]):
        if periodic:
            x = PeriodicConv2D(f, k, padding='valid', activation=activation)(x)
        else:
            x = Conv2D(f, k, padding='same', activation=activation)(x)
        if dropout > 0:
            x = Dropout(dropout)(x)
    if periodic:
        output = PeriodicConv2D(filters[-1], kernels[-1], padding='valid')(x)
    else:
        output = Conv2D(filters[-1], kernels[-1], padding='same')(x)
    model = keras.models.Model(inputs=input, outputs=output)
    return model


def main(datadir, var_dict, output_vars, filters, kernels, lr, activation, dr, batch_size, patience,
         model_save_fn, pred_save_fn, train_years, valid_years, test_years, lead_time, gpu, iterative):
    os.environ["CUDA_VISIBLE_DEVICES"]=str(gpu)
    # Limit TF memory usage
    limit_mem()

    # Open dataset and create data generators
    ds = xr.merge([xr.open_mfdataset(f'{datadir}/{var}/*.nc', combine='by_coords') for var in var_dict.keys()])

    ds_train = ds.sel(time=slice(*train_years))
    ds_valid = ds.sel(time=slice(*valid_years))
    ds_test = ds.sel(time=slice(*test_years))

    dg_train = DataGenerator(ds_train, var_dict, lead_time, batch_size=batch_size, output_vars=output_vars)
    dg_valid = DataGenerator(ds_valid, var_dict, lead_time, batch_size=batch_size, mean=dg_train.mean,
                             std=dg_train.std, shuffle=False, output_vars=output_vars)
    dg_test =  DataGenerator(ds_test, var_dict, lead_time, batch_size=batch_size, mean=dg_train.mean,
                             std=dg_train.std, shuffle=False, output_vars=output_vars)
    print(f'Mean = {dg_train.mean}; Std = {dg_train.std}')

    # Build model
    # TODO: Flexible input shapes and optimizer
    model = build_cnn(filters, kernels, input_shape=(32, 64, len(dg_train.data.level)), activation=activation, dr=dr)
    model.compile(keras.optimizers.Adam(lr), 'mse')
    print(model.summary())

    # Train model
    # TODO: Learning rate schedule
    model.fit_generator(dg_train, epochs=100, validation_data=dg_valid,
                      callbacks=[tf.keras.callbacks.EarlyStopping(
                          monitor='val_loss',
                          min_delta=0,
                          patience=patience,
                          verbose=1,
                          mode='auto'
                      )]
                      )
    print(f'Saving model weights: {model_save_fn}')
    model.save_weights(model_save_fn)


    # Create predictions
    preds = create_predictions(model, dg_test)
    print(f'Saving predictions: {pred_save_fn}')
    preds.to_netcdf(pred_save_fn)

    # Print score in real units
    # TODO: Make flexible for other states
    z500_valid = load_test_data(f'{datadir}geopotential_500', 'z')
    t850_valid = load_test_data(f'{datadir}temperature_850', 't')
    try:
        print(compute_weighted_rmse(preds.z.sel(level=500), z500_valid).load())
    except:
        print('Z500 not found in predictions.')
    try:
        print(compute_weighted_rmse(preds.t.sel(level=850), t850_valid).load())
    except:
        print('T850 not found in predictions.')

if __name__ == '__main__':
    p = ArgParser()
    p.add_argument('-c', '--my-config', is_config_file=True, help='config file path')
    p.add_argument('--datadir', type=str, required=True, help='Path to data')
    p.add_argument('--model_save_fn', type=str, required=True, help='Path to save model')
    p.add_argument('--pred_save_fn', type=str, required=True, help='Path to save predictions')
    p.add_argument('--var_dict', required=True, help='Variables: as an ugly dictionary...')
    p.add_argument('--output_vars', nargs='+', help='Output variables. Format {var}_{level}', default=None)
    p.add_argument('--filters', type=int, nargs='+', required=True, help='Filters for each layer')
    p.add_argument('--kernels', type=int, nargs='+', required=True, help='Kernel size for each layer')
    p.add_argument('--lead_time', type=int, required=True, help='Forecast lead time')
    p.add_argument('--iterative', type=bool, default=False, help='Is iterative forecast')
    p.add_argument('--iterative_max_lead_time', type=int, default=5*24, help='Max lead time for iterative forecasts')
    p.add_argument('--lr', type=float, default=1e-4, help='Learning rate')
    p.add_argument('--activation', type=str, default='elu', help='Activation function')
    p.add_argument('--dr', type=float, default=0, help='Dropout rate')
    p.add_argument('--batch_size', type=int, default=128, help='batch_size')
    p.add_argument('--patience', type=int, default=3, help='Early stopping patience')
    p.add_argument('--train_years', type=str, nargs='+', default=('1979', '2015'), help='Start/stop years for training')
    p.add_argument('--valid_years', type=str, nargs='+', default=('2016', '2016'), help='Start/stop years for validation')
    p.add_argument('--test_years', type=str, nargs='+', default=('2017', '2018'), help='Start/stop years for testing')
    p.add_argument('--gpu', type=int, default=0, help='Which GPU')
    args = p.parse_args()

    main(
        datadir=args.datadir,
        var_dict=ast.literal_eval(args.var_dict),
        output_vars=args.output_vars,
        filters=args.filters,
        kernels=args.kernels,
        lr=args.lr,
        activation=args.activation,
        dr=args.dr,
        batch_size=args.batch_size,
        patience=args.patience,
        model_save_fn=args.model_save_fn,
        pred_save_fn=args.pred_save_fn,
        train_years=args.train_years,
        valid_years=args.valid_years,
        test_years=args.test_years,
        lead_time=args.lead_time,
        gpu=args.gpu,
        iterative=args.iterative
    )
