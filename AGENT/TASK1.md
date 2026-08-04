# MiM系统的Single-Agent实现
## 需要参考的文件
1. D:\Documents\Project\Memory_in_Memory\idea_草稿\idea_v4.md，D:\Documents\Project\Memory_in_Memory\ppt\ppt1.pptx为我的idea需要参考的文件。其中如有冲突以ppt为主，我们已经打算采用gpt-4o-mini作为模型
2. D:\Documents\Project\Memory_in_Memory\idea_草稿\主流记忆基座调研报告.md，基座选择我们完全依照他来看。我们要实现一个single-agent，也就是一个简单的demo，包括基本功能和插件功能

## 你需要做的事情
1. 深入理解我的idea，如果有不明白或者你认为我表述不清晰的地方，请你进行追问，直到你认为你具备了完成任务的条件
2. 给出single-agent的基本具备的功能，以及允许其单独运行s
3. 根据这些信息，你需要为我提供一个项目文件树。也就是为我规划这个系统的架构，某个文件夹实现什么，什么功能，什么语言等
   
## 我对项目的理解
我们本质是做插件，我们先原生适配一个single-agent。这个方案不要很复杂，两侧AGENT拥有基本功能即可。重点是强调我们有一个SKILL-MAKER对失败轨迹进行迭代学习，以及我们会保存每一个版本的memory来溯源。总体来说有四个AGENT，运行测：ACCESS AGENT, CONSTRUCTION AGENT，维护侧：FAILURE AGENT, SKILL-MAKER。最后是打算对LOCOMO 6:2:2划分数据集
