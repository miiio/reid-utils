## fast-reid
借助reid模型可视化来辅助跨摄像头合并相同行人id。 

例如：将D1,D2的每个id抽取3张图片作为query，D1+D2所有图片作为gallery

可视化：/demo/visualize_result.py
## PaddleDetection跟踪模型
使用原版[PaddleDetection](https://github.com/PaddlePaddle/PaddleDetection/blob/release/2.2/configs/mot/README_cn.md)仓库的代码

替换修改过的PaddleDetection/engine/tracker.py
```bash
git clone https://github.com/PaddlePaddle/PaddleDetection.git
cp tracker.py PaddleDetection/engine/
cp run.sh PaddleDetection/
```
使用PaddleDetection提供的命令开始预测视频
```bash
CUDA_VISIBLE_DEVICES=0 python tools/infer_mot.py -c configs/mot/fairmot/fairmot_dla34_30e_1088x608.yml -o weights=https://paddledet.bj.bcebos.com/models/mot/fairmot_dla34_30e_1088x608.pdparams --video_file={your video name}.mp4 --frame_rate=25 --save_videos
```
```--frame_rate```表示视频的帧率，表示每秒抽取多少帧(经测试，减低帧率会对跟踪结果准确度有比较大的影响！)。
## tracker.py
用opencv来多线程实时加载视频和写出视频，代替原来用ffmpeg预处理生成中间图片文件的方式。

## merge脚本
根据记录的excel文件(可参考merge_D1D2.xlsx)合并相同行人的id。重新分配id并修改图片名以及excel内的id。
## Generate脚本
用于生成excel表格以及用来训练reid的行人图片

可配置项
```python#FairMOT算法输出的outpu.txt路径
output_txt_path = 'output/20211012/D02_20211012075959/mot_results/D02_20211012075959.txt' 
#原视频路径
source_video_path = '/home/swjtu/dataset/20211012/D02_20211012075959.mp4' 
#裁剪图片保存目录
save_crops_path = './output/20211012/D02_20211012075959/crops'   
#excel保存目录                                           
save_excel_path = "./output/20211012/D02_20211012075959/D02_20211012075959.xlsx" 
     
#原视频相机序号
source_video_camera_id = 1                                               
#原视频片段序号(按文件名升序从1开始排列)
source_video_seq_id = 1                                                     
#原视频真实起始日期时间
source_video_start_time = "20211012080011"   
                                      
#原视频帧率
source_video_frame_rate = 25.01     
#MOT算法检测帧率                                               
detection_frame_rate = 25.01        
#crop同一个ID的2张图片最小帧间隔                                               
crop_frame_interval = 10  
  

#忽略行人ID                                                       
ignore_pids = []
#阈值过滤
min_threshold = -1                                                                    
#最小行人框大小（在FairMOT中设置即可）
min_box_area = 20000                                                  
```

## run.sh
批处理运行这条命令
```
CUDA_VISIBLE_DEVICES=0 python tools/infer_mot.py -c configs/mot/fairmot/fairmot_dla34_30e_1088x608.yml -o weights=https://paddledet.bj.bcebos.com/models/mot/fairmot_dla34_30e_1088x608.pdparams --video_file={your video name}.mp4 --frame_rate=25 --save_videos
```