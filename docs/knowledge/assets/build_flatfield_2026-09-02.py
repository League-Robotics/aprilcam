import cv2, numpy as np, json, sys
T="/var/folders/f_/xrglkf752lxg31tz3b57_fl00000gn/T/aprilcam-mcp-7vcmk7wi/aprilcam-"
SETS = {
 "cam5_arducam-ov9281": dict(frames=["77b3c967a2a046bfbe0bb1755c6d92f0","386e62712bbc41d39c2561e019086ced","0807713c62fc48218ce68e00ce3f1965","41db3f5d29cf4a289fee9f74c5267ebf"],
        poly=[[140,90],[1128,74],[1124,748],[140,727]], mono=True, exposure=4),
 "cam3_arducam-ov9782": dict(frames=["554155bd49c3437f94b7be998f1c99a2","7275396e42bc4526adcc3aba085e6a52","86b2f6e2c28e4b338f6a0f60e9f47b76","58d9cf751bdc4b769c7eae42235e4851","9c0582576aaf4c55ba45a56d4158e5b7","165388f172d54319b527044e96f8817b","cf9e5af087254993ba5e4fac50c5eba6"],
        poly=[[140,50],[1118,40],[1138,685],[145,716]], mono=False, exposure=1),
}
for name,S in SETS.items():
    imgs=[cv2.imread(T+n+".png").astype(np.float32) for n in S["frames"]]
    means=[im.mean() for im in imgs]; clips=[(im.max(2)>=254).mean()*100 for im in imgs]
    # normalise each frame to the batch mean before averaging so flicker does not weight frames unevenly
    avg=np.mean([im*(np.mean(means)/m) for im,m in zip(imgs,means)],0)
    h,w=avg.shape[:2]
    field=np.zeros((h,w),np.uint8); cv2.fillPoly(field,[np.array(S["poly"],np.int32)],255); f=field>0
    chans = [0] if S["mono"] else [0,1,2]
    L=np.zeros((h,w,3),np.float32)
    for c in chans:
        med=cv2.medianBlur(np.clip(avg[:,:,c],0,255).astype(np.uint8),31).astype(np.float32)   # kill wrinkles/specular
        fill=np.where(f,med,med[f].mean())                                                     # no border bleed
        L[:,:,c]=cv2.GaussianBlur(fill,(0,0),40)                                               # keep only low-frequency light field
    if S["mono"]: L[:,:,1]=L[:,:,2]=L[:,:,0]
    # normalise so the field-mean of each channel maps to the same grey: fixes falloff AND colour cast together
    target=np.mean([L[:,:,c][f].mean() for c in chans])
    gain=np.clip(target/np.maximum(L,1),0.3,4.0).astype(np.float32)
    np.save(f"flatfield_gain_{name}.npy",gain)
    cv2.imwrite(f"flatfield_L_{name}.png",np.clip(L,0,255).astype(np.uint8))
    corr=np.clip(avg*gain,0,255)
    lum=lambda a:(0.299*a[:,:,2]+0.587*a[:,:,1]+0.114*a[:,:,0])
    Lb=cv2.GaussianBlur(lum(avg),(0,0),40)[f]; Lc=cv2.GaussianBlur(lum(corr),(0,0),40)[f]
    print(f"{name}: {len(imgs)} frames at exposure {S['exposure']}, per-frame mean {np.round(means,0).tolist()}, clipped% {np.round(clips,1).tolist()}")
    print(f"   light field luma p2/p50/p98 = {np.percentile(Lb,2):.0f}/{np.percentile(Lb,50):.0f}/{np.percentile(Lb,98):.0f}  spread {np.percentile(Lb,98)/np.percentile(Lb,2):.2f}x")
    if not S["mono"]:
        print("   channel means B,G,R on sheet:", [round(L[:,:,c][f].mean()) for c in range(3)], "-> colour cast removed by per-channel gain")
    print(f"   after gain map: p2/p50/p98 = {np.percentile(Lc,2):.0f}/{np.percentile(Lc,50):.0f}/{np.percentile(Lc,98):.0f}  spread {np.percentile(Lc,98)/np.percentile(Lc,2):.2f}x; gain range in field {gain[f].min():.2f}-{gain[f].max():.2f}")
    cv2.imwrite(f"flatfield_{name}_before_after.png", cv2.resize(np.hstack([avg,corr]).astype(np.uint8),(1280,400)))
    # 3x3 grid of the light field for the report
    ys=np.linspace(min(p[1] for p in S["poly"])+40,max(p[1] for p in S["poly"])-40,3).astype(int); xs=np.linspace(180,1090,3).astype(int)
    print("   luma grid NW..SE:", [[int(cv2.GaussianBlur(lum(avg),(0,0),40)[y,x]) for x in xs] for y in ys])
