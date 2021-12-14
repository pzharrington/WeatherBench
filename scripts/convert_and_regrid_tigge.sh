echo "Dir = $1"
echo "Var = $2"

# Regrid to 0.25 degrees
python ../src/regrid.py \
--input_fns "$1"/"$2" \
--output_dir "$1"/"$2"/netcdf/ \
--ddeg_out 0.25 \
--is_grib 1 \
--idx "$SLURM_PROCID"
