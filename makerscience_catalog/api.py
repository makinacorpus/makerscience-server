import json

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.conf.urls import patterns, url, include
from django.shortcuts import get_object_or_404

from dataserver.authentication import AnonymousApiKeyAuthentication
from dataserver.authorization import GuardianAuthorization
from datetime import datetime
from graffiti.api import TaggedItemResource
from guardian.shortcuts import assign_perm, get_objects_for_user
from projects.api import ProjectResource
from projectsheet.api import ProjectSheetResource

from haystack.query import SearchQuerySet

from taggit.models import Tag
from tastypie import fields
from tastypie.authorization import DjangoAuthorization
from tastypie.constants import ALL_WITH_RELATIONS
from tastypie.resources import ModelResource
from tastypie.utils import trailing_slash


from .models import MakerScienceProject, MakerScienceResource
from makerscience_profile.api import MakerScienceProfileResource
from accounts.models import ObjectProfileLink, Profile
from projectsheet.models import ProjectSheet

class MakerScienceAPIAuthorization(GuardianAuthorization):

    def read_detail(self, object_list, bundle):
        """
        For MakerScienceResources we let anyone authenticated or not read detail 
        """
        self.generic_base_check(object_list, bundle)
        return True

    def read_list(self, object_list, bundle):
        """
        For MakerScienceResources we let anyone authenticated or not read list 
        """
        self.generic_base_check(object_list, bundle)
        return object_list

    def create_detail(self, object_list, bundle):
        """
        For MakerScienceResources we let anyone with add permissions
        *FIXME* : this override should not be required since we assign global edit
        rights to all new users (see .models.py)
    
        """
        self.generic_base_check(object_list, bundle)
        return bundle.request.user.has_perm(self.create_permission_code)

class MakerScienceProjectAuthorization(MakerScienceAPIAuthorization):
    def __init__(self):
        super(MakerScienceProjectAuthorization, self).__init__(
            create_permission_code="makerscience_catalog.add_makerscienceproject",
            view_permission_code="makerscience_catalog.view_makerscienceproject",
            update_permission_code="makerscience_catalog.change_makerscienceproject",
            delete_permission_code="makerscience_catalog.delete_makerscienceproject"
        )

class MakerScienceProjectResource(ModelResource):
    parent = fields.ToOneField(ProjectResource, 'parent', full=True)
    tags = fields.ToManyField(TaggedItemResource, 'tagged_items', full=True, null=True)

    base_projectsheet = fields.ToOneField(ProjectSheetResource, 'parent__projectsheet', null=True, full=True)

    linked_resources = fields.ToManyField('makerscience_catalog.api.MakerScienceResourceResource', 'linked_resources', full=True,null=True)

    class Meta:
        queryset = MakerScienceProject.objects.all()
        allowed_methods = ['get', 'post', 'put', 'patch']
        resource_name = 'makerscience/project'
        authentication = AnonymousApiKeyAuthentication()
        authorization = MakerScienceProjectAuthorization()
        always_return_data = True
        filtering = {
            'parent' : ALL_WITH_RELATIONS,
            'featured' : ['exact'],
        }

    def dehydrate(self, bundle):
        try:
            link = ObjectProfileLink.objects.get(content_type=ContentType.objects.get_for_model(bundle.obj.parent),
                                                   object_id=bundle.obj.parent.id,
                                                   level=0)
            profile = link.profile.makerscienceprofile_set.all()[0]

            bundle.data["by"] = {
                'profile_id' : profile.id,
                'profile_email' : profile.parent.user.email,
                'full_name' : "%s %s" % (profile.parent.user.first_name, profile.parent.user.last_name)
            }
        except Exception, e:
            pass
        return bundle

    def hydrate(self, bundle):
        bundle.data["modified"] = datetime.now()
        return bundle

    def prepend_urls(self):
        """
        URL override for when giving change perm to a user profile passed as parameter profile_id
        """
        return [
           url(r"^(?P<resource_name>%s)/(?P<ms_id>\d+)/assign%s$" % 
                (self._meta.resource_name, trailing_slash()),
                 self.wrap_view('ms_project_edit_assign'), name="api_ms_project_assign"),
           url(r"^(?P<resource_name>%s)/(?P<ms_id>\d+)/check/(?P<user_id>\d+)%s$" % 
                (self._meta.resource_name, trailing_slash()),
                 self.wrap_view('ms_project_check_edit_perm'), name="api_ms_project_check_edit_perm"),
           url(r"^(?P<resource_name>%s)/search%s$" % (self._meta.resource_name, 
                                trailing_slash()), self.wrap_view('ms_project_search'), name="api_ms_project_search"),

        ]

    def ms_project_search(self, request, **kwargs):
        self.method_check(request, allowed=['get'])
        self.throttle_check(request)
        self.is_authenticated(request)

        # Query params
        query = request.GET.get('q', '')
        selected_facets = request.GET.getlist('facet', None)
        
        sqs = SearchQuerySet().models(MakerScienceProject).facet('tags')
        # narrow down QS with facets
        if selected_facets:
            for facet in selected_facets:
                sqs = sqs.narrow('tags:%s' % (facet))
        # launch query
        if query != "":
            sqs = sqs.auto_query(query)
        # build object list
        objects = []
        for result in sqs:
            bundle = self.build_bundle(obj=result.object, request=request)
            bundle = self.full_dehydrate(bundle)
            objects.append(bundle)
        object_list = {
            'objects': objects,
        }

        self.log_throttled_access(request)
        return self.create_response(request, object_list)

    #FIXME / DRY me out !
    def ms_project_edit_assign(self, request, **kwargs):
        """
        Method to assign edit permissions for a MSResource or MSProject 'ms_id' to
        a user with id passed as POST parameter 'user_id' 
        """
        self.method_check(request, allowed=['post'])
        self.throttle_check(request)
        self.is_authenticated(request)
       
        target_user_id = json.loads(request.body)['user_id']
        target_user = get_object_or_404(User, pk=target_user_id)
        target_object_id = kwargs['ms_id']
        # FIXME : for better security, we should also check that request.user has edit rights on target object
        # => which implies that change rigths are automatically given to creator of a sheet
        target_object = get_object_or_404(MakerScienceProject, pk=target_object_id)
        assign_perm("change_makerscienceproject", user_or_group=target_user, obj=target_object)
        
        return self.create_response(request, {'Change rights on MakerScienceProject assigned to given profile':True})

    def ms_project_check_edit_perm(self, request, **kwargs):
        """
        Method to check edit permissions for a given user_id
        """   
        self.method_check(request, allowed=['get', 'post'])
        self.throttle_check(request)
        self.is_authenticated(request)

        user_id = kwargs['user_id']
        user = get_object_or_404(User, pk=user_id)
        
        target_object_id = kwargs['ms_id']
        target_object = get_object_or_404(MakerScienceProject, pk=target_object_id)
        if user.has_perm('change_makerscienceproject', target_object):
            return self.create_response(request, {'has_perm':True})
        else:
            return self.create_response(request, {'has_perm':False})

class MakerScienceResourceAuthorization(MakerScienceAPIAuthorization):
    def __init__(self):
        super(MakerScienceResourceAuthorization, self).__init__(
            create_permission_code="makerscience_catalog.add_makerscienceresource",
            view_permission_code="makerscience_catalog.view_makerscienceresource",
            update_permission_code="makerscience_catalog.change_makerscienceresource",
            delete_permission_code="makerscience_catalog.delete_makerscienceresource"
        )

class MakerScienceResourceResource(ModelResource):
    parent = fields.ToOneField(ProjectResource, 'parent', full=True)
    tags = fields.ToManyField(TaggedItemResource, 'tagged_items', full=True, null=True)

    base_resourcesheet = fields.ToOneField(ProjectSheetResource, 'parent__projectsheet', null=True, full=True)

    linked_resources = fields.ToManyField('makerscience_catalog.api.MakerScienceResourceResource', 'linked_resources', full=True,null=True)

    class Meta:
        queryset = MakerScienceResource.objects.all()
        allowed_methods = ['get', 'post', 'put', 'patch']
        resource_name = 'makerscience/resource'
        authentication = AnonymousApiKeyAuthentication()
        authorization = MakerScienceResourceAuthorization()
        always_return_data = True
        filtering = {
            'parent' : ALL_WITH_RELATIONS,
            'featured' : ['exact'],
        }

    def dehydrate(self, bundle):
        try:
            link = ObjectProfileLink.objects.get(content_type=ContentType.objects.get_for_model(bundle.obj.parent),
                                                   object_id=bundle.obj.parent.id,
                                                   level=0)
            profile = link.profile.makerscienceprofile_set.all()[0]

            bundle.data["by"] = {
                'profile_id' : profile.id,
                'profile_email' : profile.parent.user.email,
                'full_name' : "%s %s" % (profile.parent.user.first_name, profile.parent.user.last_name)
            }
        except Exception, e:
            pass
        return bundle

    def hydrate(self, bundle):
        bundle.data["modified"] = datetime.now()
        return bundle

    def prepend_urls(self):
        """
        URL override for when giving change perm to a user profile passed as parameter profile_id
        """
        return [
           url(r"^(?P<resource_name>%s)/(?P<ms_id>\d+)/assign%s$" % 
                (self._meta.resource_name, trailing_slash()),
                 self.wrap_view('ms_resource_edit_assign'), name="api_ms_resource_assign"),
           url(r"^(?P<resource_name>%s)/(?P<ms_id>\d+)/check/(?P<user_id>\d+)%s$" % 
                (self._meta.resource_name, trailing_slash()),
                 self.wrap_view('ms_resource_check_edit_perm'), name="api_ms_resource_check_edit_perm"),
           url(r"^(?P<resource_name>%s)/search%s$" % (self._meta.resource_name, 
                trailing_slash()), self.wrap_view('ms_resource_search'), name="api_ms_resource_search"),
        ]

    def ms_resource_search(self, request, **kwargs):
        self.method_check(request, allowed=['get'])
        self.throttle_check(request)
        self.is_authenticated(request)

        # Query params
        query = request.GET.get('q', '')
        selected_facets = request.GET.getlist('facet', None)
        
        sqs = SearchQuerySet().models(MakerScienceResource).facet('tags')
        # narrow down QS with facets
        if selected_facets:
            for facet in selected_facets:
                sqs = sqs.narrow('tags:%s' % (facet))
        # launch query
        if query != "":
            sqs = sqs.auto_query(query)
        # build object list
        objects = []
        for result in sqs:
            bundle = self.build_bundle(obj=result.object, request=request)
            bundle = self.full_dehydrate(bundle)
            objects.append(bundle)
        object_list = {
            'objects': objects,
        }

        self.log_throttled_access(request)
        return self.create_response(request, object_list)

    #FIXME / DRY me out !
    def ms_resource_edit_assign(self, request, **kwargs):
        """
        Method to assign edit permissions for a MSResource 'ms_id' to
        a user with profile passed as POST parameter 'profile_id' 
        """
        self.method_check(request, allowed=['post'])
        self.throttle_check(request)
        self.is_authenticated(request)
       
        target_user_id = json.loads(request.body)['user_id']
        target_user = get_object_or_404(User, pk=target_user_id)
        target_object_id = kwargs['ms_id']
        # FIXME : for better security, we should also check that request.user has edit rights on target object
        # => which implies that change rigths are automatically given to creator of a sheet
        target_object = get_object_or_404(MakerScienceResource, pk=target_object_id)
        assign_perm("change_makerscienceresource", user_or_group=target_user, obj=target_object)
        
        return self.create_response(request, {'Change rights on MakerScienceResource assigned to given user':True})

    def ms_resource_check_edit_perm(self, request, **kwargs):
        """
        Method to check edit permissions for a given user_id
        """   
        self.method_check(request, allowed=['get', 'post'])
        self.throttle_check(request)
        self.is_authenticated(request)

        user_id = kwargs['user_id']
        user = get_object_or_404(User, pk=user_id)
        target_object_id = kwargs['ms_id']
        target_object = get_object_or_404(MakerScienceResource, pk=target_object_id)
        if user.has_perm('change_makerscienceresource', target_object):
            return self.create_response(request, {'has_perm':True})
        else:
            return self.create_response(request, {'has_perm':False})

