import h5py
#from mpi4py import MPI
import numpy as np
import time
from netCDF4 import Dataset as DS
import os

def writetofile(src, dest, channel_idx, varslist):
    if os.path.isfile(src):
        batch = 2**6
        rank = MPI.COMM_WORLD.rank
        Nproc = MPI.COMM_WORLD.size
        Nimgtot = 1460#src_shape[0]

        Nimg = Nimgtot//Nproc
        print("Nimgtot",Nimgtot)
        print("Nproc",Nproc)
        print("Nimg",Nimg)
        base = rank*Nimg
        end = (rank+1)*Nimg if rank<Nproc - 1 else Nimgtot
        idx = base

        for variable_name in varslist:

            fsrc = DS(src, 'r', format="NETCDF4").variables[variable_name]
            fdest = h5py.File(dest, 'a', driver='mpio', comm=MPI.COMM_WORLD)

            start = time.time()
            while idx<end:
                if end - idx < batch:
                    ims = fsrc[idx:end]
                    print(ims.shape)
                    fdest['fields'][idx:end, channel_idx, :, :] = ims
                    break
                else:
                    ims = fsrc[:]
                    fdest['fields'][idx:idx+batch, channel_idx, :, :] = ims
                    idx+=batch
                    ttot = time.time() - start
                    eta = (end - base)/((idx - base)/ttot)
                    hrs = eta//3600
                    mins = (eta - 3600*hrs)//60
                    secs = (eta - 3600*hrs - 60*mins)

            ttot = time.time() - start
            hrs = ttot//3600
            mins = (ttot - 3600*hrs)//60
            secs = (ttot - 3600*hrs - 60*mins)
            channel_idx += 1

years = np.arange(1979, 2020)
months = np.arange(1,12)
base = '/global/cscratch1/sd/pharring/ERA5/precip/tigge/'
var = 'total_precipitation'
vartag = 'tp'

#rank = MPI.COMM_WORLD.rank
#Nproc = MPI.COMM_WORLD.size
#month = months[rank]
#year = 2018
allifs= []
for month in months:
    print(month)
    src = base+var+'/netcdf/total_precipitation_2016_%02d_raw_0.25deg.nc'%month
    ifs = DS(src, 'r', format="NETCDF4").variables[vartag][...]
    ifs = ifs.astype(np.float32)
    ifs = ifs[:,:,:,:]/1e3 # convert to m units
    ifs = np.diff(ifs[:, :, :, :], axis=1)  # de-accumulate so each timestep shows 6hr precip
    allifs.append(ifs)

allifs = np.concatenate(allifs, axis=0)

dest = '/global/cscratch1/sd/pharring/ERA5/precip/tigge/' + var + '/2016.h5'
print('array shape:', allifs.shape)
print('writing to %s'%dest)
with h5py.File(dest, 'a') as f:
    f.create_dataset(vartag, data=allifs)


