"""Read-only built-in character metadata and regular sprite atlas geometry."""
from pathlib import Path
from .storage import read_json

STATES=('idle','thinking','working','waiting','completed','error')


def character_spec(name, assets=None):
    if name not in ('tabby','catgirl','robot'):
        return None
    assets=Path(assets) if assets else Path(__file__).resolve().parents[1]/'assets'
    folder=assets/'characters'/name
    obj=read_json(folder/'character.json')
    if not isinstance(obj,dict) or obj.get('image') not in ('atlas.png','image.png'):
        return None
    columns,rows=obj.get('columns'),obj.get('rows')
    if type(columns) is not int or type(rows) is not int or not 1<=columns<=8 or not 1<=rows<=8:
        return None
    indices=obj.get('states')
    if not isinstance(indices,dict) or any(type(indices.get(s)) is not int or not 0<=indices[s]<columns*rows for s in STATES):
        return None
    image=folder/obj['image']
    if not image.is_file():
        return None
    result={'image':image,'columns':columns,'rows':rows,'states':indices,'name':obj.get('name',name)}
    # Only bundled assets are resolved here; imported packs use RoleLibrary's
    # full archive and image validation instead.
    clips={}
    for key,clip in obj.get('clips',{}).items():
        if key not in STATES+('head_pat','poke','tail','drag_start','drag_end','wave') or not isinstance(clip,dict):
            continue
        relative=clip.get('file')
        if not isinstance(relative,str) or type(clip.get('loop')) is not bool:
            continue
        path=folder/relative
        if path.suffix.lower() in ('.gif','.webp') and path.resolve().is_relative_to(folder.resolve()) and path.is_file():
            clips[key]={'file':path,'loop':clip['loop']}
    if clips:result['clips']=clips
    return result


def atlas_rect(width,height,columns,rows,index):
    if width<=0 or height<=0 or not 0<=index<columns*rows:
        raise ValueError('Invalid character atlas geometry')
    column,row=index%columns,index//columns
    x1,x2=round(width*column/columns),round(width*(column+1)/columns)
    y1,y2=round(height*row/rows),round(height*(row+1)/rows)
    return x1,y1,x2-x1,y2-y1
