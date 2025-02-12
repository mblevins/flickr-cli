#! /bin/python
# CLI for flickr APIs
import flickrapi
import click
import os
import logging
import markdown
from functools import wraps
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import exif
import json


flickr=None
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("flickr-cli")


def command_wrapper(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except flickrapi.FlickrError as e:
            log.error(f"Flicker API returned {e.code}")
            log.debug(traceback.format_exc())
    return wrapper

def get_or_die( envname ):
    """Get an env var, or die trying
    """
    value=os.getenv( envname )
    if (not value):
        log.error(f"Missing env var: {envname}")
        exit(1)
    return value

def authorize_helper(perms):
    """Get an an authorization token
    """
    if not flickr.token_valid(perms=perms):

        flickr.get_request_token(oauth_callback='oob')
        authorize_url = flickr.auth_url(perms=perms)

        print( f"""
            Use a browser to go to {authorize_url}, then copy the code 
            to the prompt
            """)

        # Get the verifier code from the user. Do this however you
        # want, as long as the user gives the application the code.
        verifier = str(input('Verifier code: '))

        # Trade the request token for an access token
        flickr.get_access_token(verifier)
    else:
        log.debug("Stored access token is valid")

def find_album( albumname ):
    """find an album ID
    Must have called authorize_helper before this, read or write
    """
    resp = flickr.photosets.getList()
    for a in resp.find("photosets").findall('photoset'):
        if (a.find('title').text == albumname):
            return a.get('id')
    return None

@click.group()
@click.option('--debug/--no-debug', '-d', help='Turn on debug statements',default=False)
def cli(debug):
    # todo: turn on root DEBUG
    root = logging.getLogger()
    root.setLevel(logging.ERROR)
    if (debug):
        log.setLevel(logging.DEBUG)


@cli.command()
@click.option('--album', type=click.STRING, help='Album to use')
@click.option('--output', type=click.File('w'), help='HTML file to write ')
@command_wrapper
def createphotopage(album, output):
    """Create a photo page of links
    """
    authorize_helper('read')
    album_id=find_album( album )
    if not album_id:
        print("Can't find album")
        return(1)
    pictures=flickr.photosets.getPhotos(photoset_id=album_id)
    text=""
    for p in pictures.find("photoset").findall("photo"):
        # see https://www.flickr.com/services/api/misc.urls.html for this magic incantation
        smallurl=f"https://live.staticflickr.com/{p.get('server')}/{p.get('id')}_{p.get('secret')}.jpg"
        bigurl=f"https://live.staticflickr.com/{p.get('server')}/{p.get('id')}_{p.get('secret')}_b.jpg"
        text=text + f"[![Image]({smallurl})]({bigurl})\n\n"
        text=text + f"```[![Image]({smallurl})]({bigurl})```\n\n"
    print(markdown.markdown(text),file=output)

@cli.command()
@command_wrapper
def listphotos():
    """List photos and their IDs
    """
    authorize_helper('read')
    resp = flickr.photosets.getList()
    for a in resp.find("photosets").findall('photoset'):
        print(f"{a.find('title').text},{a.get('id')}")

@cli.command()
@command_wrapper
def listalbums():
    """List albums and their IDs
    """
    authorize_helper('read')
    listObj = flickr.photosets.getList()
    resp={'data': []}
    for a in listObj.find("photosets").findall('photoset'):
        resp['data'].append( { 'title': a.find('title').text, 'id': a.get('id') })
    print( json.dumps( resp, indent=2 ) )

@cli.command()
@command_wrapper
def listtags():
    """List all tags 
    """
    authorize_helper('read')
    listObj = flickr.tags.getListUser()
    resp={'data': []}
    for t in listObj.find("who").find("tags").findall('tag'):
        resp['data'].append( t.text )
    print( json.dumps( resp, indent=2 ) )

@cli.command()
@click.option('--level', type=click.Choice(['read', 'write']), default='write', help="level of auth to get")
@command_wrapper
def authorize(level):
    """Explicit authorization 
    """
    authorize_helper(level)
    resp=flickr.test.login()
    print(f"username={resp.find('user').find('username').text}")


def upload_worker(fileobj, album_id, tags):
    # exceptions thrown will be caught in as_completed loop
    fullpath=os.path.join( fileobj['path'], fileobj['file'])
    try:
        step="Upload"
        print(f"Uploading {fullpath} {fileobj['file']}")
        uploadobj=flickr.upload(filename=fullpath, 
                            title=fileobj['file'],
                            tags=tags)
        photo_id=uploadobj.find('photoid')
        step="Add Album"
        log.debug(f"Adding photo {photo_id.text} to album {album_id}")
        flickr.photosets.addPhoto(photo_id=photo_id.text, photoset_id=album_id)
    except flickrapi.FlickrError as e:
        log.error(f"During upload of {fullpath}, step {step}, FlickrError {e.code}")
        raise
    except Exception as e:
        log.error(f"During upload of {fullpath}, step {step}, Exception {e}")
        raise
    return True
 
@cli.command()
@click.option('--dir', type=click.STRING, default=".", help="Directory to upload", required=True)
@click.option('--album', type=click.STRING, default=".", help="Album to add to", required=True)
@click.option('--tags', type=click.STRING, default=".", help="Space separated list of tags", required=True)
@click.option('--numworkers', type=click.INT, default="5", help="Num workers to upload")
@command_wrapper
def upload(dir, numworkers, album, tags):
    """Upload a directory of files
    """
    if (not os.path.isdir( dir )):
        log.error(f"{dir} isn't a directory")

    authorize_helper('write')

    # This kind of a pain, need to upload one photo as that is required to create an album,
    # then upload the rest of the photos and associate to the album.

    # probably a clever map way of doing this
    # fileobj_list is a list of objects with "path" and "file"
    fileobj_list=[]
    for path, pdir, files in os.walk(dir):
        for f in files:
            fileobj_list.append({'path': path, 'file': f})

    # grab the first file and use it to create an album/photoset
    fileobj=fileobj_list.pop()

    uploadobj=flickr.upload(filename=os.path.join( fileobj['path'], fileobj['file']),
                           title=fileobj['file'],
                           tags=tags)
    photo_id=uploadobj.find('photoid')
    log.debug(f"Creating photoset {album} with photo {photo_id}")
    albumobj=flickr.photosets.create(title=album, primary_photo_id=photo_id.text)
    album_id=albumobj.find("photoset").attrib["id"]
    log.debug(f"Album {album} created with id {album_id}")

    # Album/photoset is created, now do the rest
    num_success=0
    num_fail=0
    log.debug(f"Submitting threads")
    with ThreadPoolExecutor(max_workers=numworkers) as executor:
        # note the "with" will wait for all threads to finish, although
        # as_completed will also wait
        futures=[]
        for fileobj in fileobj_list:
            futures.append(executor.submit( upload_worker, fileobj, album_id, tags ))

        for future in as_completed(futures):
            try:
                future.result()
                # if future thrw an exception, won't reach next line
                num_success=num_success+1
            except Exception as e:
                log.error(f"Uploader returned exception {e}")
                num_fail=num_fail+1
    print(f"{num_success} uploaded, {num_fail} failed")
    return num_fail


def inspect_helper( dir, exif_dict ):
    for path, dir, files in os.walk(dir):
        for f in files:
            path=os.path.join( path, dir, f )
            if (f.lower().endswith(".jpg")):
                with open(path, 'rb') as image_file:
                    try:
                        image = exif.Image(image_file)
                        if not image.has_exif:
                            log.warning(f"{path} does not have exif data")
                        else:
                            exif_obj={'path': path, 'datetime': image.get('datetime_original'), 
                                'make': image.get('model')}
                            exif_dict.append( exif_obj )
                        doneone=True
                    except Exception as e:
                        log.error(f"exception reading {path}")
                        log.error(e)

@cli.command()
@click.option("--dir", type=click.STRING, default=".")
@click.option("--outputjson", type=click.File('w'), required=True)
def inspect( dir, outputjson):
    """Inspect all photo directories and create a json file with contents
    (This can take a while)
    """
    exif_dict=[]
    if (not os.path.isdir( dir )):
        log.error(f"{dir} isn't a directory")
    inspect_helper( dir, exif_dict )
    json.dump( {'data': exif_dict }, outputjson, indent=2)


def rename_helper( collection, album, photo_year ):
    """Come up with a new album name
    """
    # replace spaces with dash
    if album.find(" ") != -1:
        album=album.replace(" ","-")

    # replace underscores with dash
    if album.find("_") != -1:
        album=album.replace("_","-")

    # encode year
    # year may be off from photo year
    found_likely_date = re.search(r"([12][789012][0-9][0-9])",album)
    if (found_likely_date):
        # if we found a date, remove it
        found_index=found_likely_date.start()
        if (found_likely_date.group(1) != photo_year):
            log.error(f"{collection}/{album} had encoded date, didn't match photo date of {photo_year}")
            return None     
        # date is encoded, but in wrong place
        if found_index == len(album) - 4:
            # year is at end, remove it
            if (album[found_index-1] != "-"):
                log.error(f"{collection}/{album} has weird encoded date")
                return None
            album=album.replace(f"-{photo_year}","",1)
        elif album[found_index+4] == "-":
            # year is in middle, remove it
            album=album.replace(f"{photo_year}-","",1)
        else:
            # dunno
            log.error(f"{collection}/{album} has weird encoded date")
            return None
    album=f"{photo_year}-{album}"        

    return( album )

@cli.command()
@click.option("--inputjson", type=click.File('r'), required=True)
@click.option("--dir", type=click.STRING, default=".", help="leading path containing collection folder") 
def rename( inputjson, dir  ):
    """Given a json file from inspect, create a script to rename directories
    """
    files=json.load( inputjson )['data']

    # collection, album, file
    path_ex = re.compile(r"^{}/(.*)/(.*)/(.*)\.jpg".format( dir ),re.IGNORECASE)
    date_ex = re.compile(r"^([0-9]{4}):")
    collection=""
    album_oldname=""

    # This processes a set of file objects, guaranteed to be in pathorder. 
    for f in files:
        path_match=path_ex.match(f['path'] )
        if not path_match:
            log.error(f"Can't parse {f['path']}")
            continue
        if (path_match.group(1) == collection and path_match.group(2) == album_oldname):
            continue
        collection=path_match.group(1)
        album_oldname = path_match.group(2)
        if (f.get('datetime')):
            date_match=date_ex.match(f['datetime'])
            if not date_match:
                log.error(f"Can't parse {f['datetime']}")
                continue   
            year=date_match.group(1)
        else:
            year="1970"

        album_newname = rename_helper(collection, album_oldname, year)
        
        if (album_newname):
            if (album_newname == album_oldname):
                print( f"# {collection}/{album_oldname} didn't change")
            else:
                print( f"mv {collection}/{album_oldname} {collection}/{album_newname}")

if __name__ == '__main__':
    api_key=get_or_die( "FLICKR_API_KEY" )
    api_secret=get_or_die( "FLICKR_API_SECRET" )
    flickr = flickrapi.FlickrAPI(api_key, api_secret)
    cli()


