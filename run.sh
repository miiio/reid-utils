#!/bin/bas
video_list=(
    /home/swjtu/dataset/20211012/D01_20211012122308.mp4
    /home/swjtu/dataset/20211012/D01_20211012133139.mp4
    /home/swjtu/dataset/20211012/D01_20211012144009.mp4
    /home/swjtu/dataset/20211012/D01_20211012155000.mp4
)
for v in ${video_list[*]}
do
    CUDA_VISIBLE_DEVICES=0 python tools/infer_mot.py -c configs/mot/fairmot/fairmot_dla34_30e_1088x608.yml \
    --video_file=${v} --output_dir=output/$(basename ${v} .mp4)/ \
    --save_videos --draw_threshold 0.5 --frame_rate 25
done