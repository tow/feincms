import re

from django import forms, template
from django.conf import settings
from django.contrib import admin
from django.contrib.admin.util import unquote
from django.core import serializers
from django.db import connection, transaction
from django.forms.formsets import all_valid
from django.forms.models import inlineformset_factory
from django.http import HttpResponseRedirect, HttpResponse
from django.shortcuts import render_to_response
from django.utils import simplejson
from django.utils.functional import update_wrapper
from django.utils.translation import ugettext_lazy as _

from feincms.models import Region, Template, Page, PageContent

from django.contrib.admin.options import IncorrectLookupParameters

FEINCMS_ADMIN_MEDIA = getattr(settings, 'FEINCMS_ADMIN_MEDIA', '/media/sys/feincms/')


class PageForm(forms.ModelForm):
    class Meta:
        model = Page


class PageSettingsFieldset(forms.ModelForm):
    # This form class is used solely for presentation, the data will be saved
    # by the PageForm above

    class Meta:
        model = Page
        exclude = ('active', 'template', 'title', 'in_navigation')


class PageAdmin(admin.ModelAdmin):
    # the fieldsets config here is used for the add_view, it has no effect
    # for the change_view which is completely customized anyway
    fieldsets = (
        (None, {
            'fields': ('active', 'in_navigation', 'template', 'title', 'slug',
                'parent', 'language'),
        }),
        (_('Other options'), {
            'classes': ('collapse',),
            'fields': ('override_url', 'meta_keywords', 'meta_description'),
        }),
        )
    list_display=('__unicode__', 'active', 'in_navigation',
        'language', 'template')
    list_filter=('active', 'in_navigation', 'language', 'template')
    search_fields = ('title', 'slug', '_content_title', '_page_title',
        'meta_keywords', 'meta_description')
    prepopulated_fields={
        'slug': ('title',),
        }

    def get_urls(self):
        from django.conf.urls.defaults import patterns, url

        def wrap(view):
            def wrapper(*args, **kwargs):
                return self.admin_site.admin_view(view)(*args, **kwargs)
            return update_wrapper(wrapper, view)

        info = self.admin_site.name, self.model._meta.app_label, self.model._meta.module_name

        urlpatterns = patterns('',
            url(r'^$',
                wrap(self.changelist_view),
                name='%sadmin_%s_%s_changelist' % info),
            url(r'^add/$',
                wrap(self.add_view),
                name='%sadmin_%s_%s_add' % info),
            url(r'^(.+)/history/$',
                wrap(self.history_view),
                name='%sadmin_%s_%s_history' % info),
            url(r'^(.+)/delete/$',
                wrap(self.delete_view),
                name='%sadmin_%s_%s_delete' % info),
            url(r'^save-pagetree/$', wrap(self.save_pagetree)),
            url(r'^delete-page-ajax/$', wrap(self.delete_page_ajax)),
            url(r'^(.+)/$',
                wrap(self.change_view),
                name='%sadmin_%s_%s_change' % info),
        )

        return urlpatterns


    inline_formset_types = [(
        content_type,
        inlineformset_factory(Page, content_type, extra=1)
        ) for content_type in PageContent.types]


    def change_view(self, request, object_id, extra_context=None):
        opts = self.model._meta
        page = self.model._default_manager.get(pk=unquote(object_id))

        if not self.has_change_permission(request, page):
            raise PermissionDenied

        if request.method == 'POST':
            page_form = PageForm(request.POST, request.FILES, instance=page)

            inline_formsets = [
                formset_class(request.POST, request.FILES, instance=page,
                    prefix=content_type.__name__.lower())
                for content_type, formset_class in self.inline_formset_types]

            if page_form.is_valid() and all_valid(inline_formsets):
                page_form.save()
                for formset in inline_formsets:
                    formset.save()
                return HttpResponseRedirect(".")

            settings_fieldset = PageSettingsFieldset(request.POST, instance=page)
            settings_fieldset.is_valid()
        else:
            page_form = PageForm(instance=page)
            inline_formsets = [
                formset_class(instance=page, prefix=content_type.__name__.lower())
                for content_type, formset_class in self.inline_formset_types]

            settings_fieldset = PageSettingsFieldset(instance=page)

        content_types = []
        for content_type in PageContent.types:
            content_name = content_type._meta.verbose_name
            content_types.append((content_name, content_name.replace(' ','')))

        context = {
            'has_file_field': True, # FIXME - but isn't fixed in django either
            'opts': opts,
            'page': page,
            'page_form': page_form,
            'inline_formsets': inline_formsets,
            'content_types': content_types,
            'settings_fieldset': settings_fieldset,
            'FEINCMS_ADMIN_MEDIA': FEINCMS_ADMIN_MEDIA,
        }

        return render_to_response("admin/feincms/page/change_form_edit.html",
            context, context_instance=template.RequestContext(request))

    def changelist_view(self, request, extra_context=None):
        from django.contrib.admin.views.main import ChangeList, ERROR_FLAG
        opts = self.model._meta
        app_label = opts.app_label

        if not self.has_change_permission(request, None):
            raise PermissionDenied
        try:
            cl = ChangeList(request, self.model, self.list_display, self.list_display_links, self.list_filter,
                self.date_hierarchy, self.search_fields, self.list_select_related, self.list_per_page, self)
        except IncorrectLookupParameters:
            # Wacky lookup parameters were given, so redirect to the main
            # changelist page, without parameters, and pass an 'invalid=1'
            # parameter via the query string. If wacky parameters were given and
            # the 'invalid=1' parameter was already in the query string, something
            # is screwed up with the database, so display an error page.
            if ERROR_FLAG in request.GET.keys():
                return render_to_response('admin/invalid_setup.html', {'title': _('Database error')})
            return HttpResponseRedirect(request.path + '?' + ERROR_FLAG + '=1')

        context = {
            'FEINCMS_ADMIN_MEDIA': FEINCMS_ADMIN_MEDIA,
            'title': cl.title,
            #'pages_old': Page.objects.all().order_by('parent__id'),
            'is_popup': cl.is_popup,
            'cl': cl,
            'has_add_permission': self.has_add_permission(request),
            'root_path': self.admin_site.root_path,
            'app_label': app_label,
        }
        context.update(extra_context or {})
        return render_to_response("admin/feincms/page/change_list_edit.html",
            context, context_instance=template.RequestContext(request))

    def save_pagetree(self, request):
        pagetree = simplejson.loads(request.POST['tree'])
        # 0 = tree_id, 1 = parent_id, 2 = left, 3 = right, 4 = level, 5 = page_id
        sql = "UPDATE %s SET %s=%%s, %s_id=%%s, %s=%%s, %s=%%s, %s=%%s WHERE %s=%%s" % (
            Page._meta.db_table,
            Page._meta.tree_id_attr,
            Page._meta.parent_attr,
            Page._meta.left_attr,
            Page._meta.right_attr,
            Page._meta.level_attr,
            Page._meta.pk.column)

        connection.cursor().executemany(sql, pagetree)
        transaction.commit_unless_managed()

        return HttpResponse("OK", mimetype="text/plain")

    def delete_page_ajax(self, request):
        page_id = request.POST['page-id']
        obj = self.model._default_manager.get(pk=unquote(page_id))
        obj.delete()
        return HttpResponse("OK", mimetype="text/plain")


admin.site.register(Region,
    list_display=('key', 'inherited'),
    )
admin.site.register(Template,
    list_display=('title', 'path'),
    )
admin.site.register(Page, PageAdmin)

