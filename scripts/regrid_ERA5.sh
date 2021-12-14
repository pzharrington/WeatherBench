echo "Dir = $1"
echo "Var = $2"
echo "idx = $SLURM_PROCID"

# Regrid to 0.25 degrees
python ../src/regrid.py \
--input_fns "$1" \
--output_dir "$SCRATCH"/ERA5/precip/"$2"/netcdf/ \
--ddeg_out 0.25 \
--idx "$SLURM_PROCID" \
--is_grib 1
