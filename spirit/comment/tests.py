# -*- coding: utf-8 -*-

from __future__ import unicode_literals

import os
import json
import shutil

from django.test import TestCase, RequestFactory
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.template import Template, Context
from django.core.exceptions import PermissionDenied
from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.utils import override_settings
from django.utils.six import BytesIO

from ..core.tests import utils
from .models import Comment
from .forms import CommentForm, CommentMoveForm, CommentImageForm
from .tags import render_comments_form
from ..core.utils import markdown
from .views import delete as comment_delete
from ..topic.models import Topic
from ..category.models import Category
from ..user.models import UserProfile
from .history.models import CommentHistory
from .utils import comment_posted, pre_comment_update, post_comment_update
from ..topic.notification.models import TopicNotification, MENTION
from ..topic.unread.models import TopicUnread
from .poll.models import CommentPoll
from . import views

User = get_user_model()


class CommentViewTest(TestCase):

    def setUp(self):
        cache.clear()
        self.user = utils.create_user()
        self.category = utils.create_category()
        self.topic = utils.create_topic(category=self.category, user=self.user)

    def test_comment_publish(self):
        """
        create comment
        """
        utils.login(self)
        form_data = {'comment': 'foobar', }
        response = self.client.post(reverse('spirit:comment:publish', kwargs={'topic_id': self.topic.pk, }),
                                    form_data)
        expected_url = reverse('spirit:comment:find', kwargs={'pk': 1, })
        self.assertRedirects(response, expected_url, status_code=302, target_status_code=302)
        self.assertEqual(len(Comment.objects.all()), 1)

        # ratelimit
        response = self.client.post(reverse('spirit:comment:publish', kwargs={'topic_id': self.topic.pk, }),
                                    form_data)
        self.assertEqual(len(Comment.objects.all()), 1)

        # get
        response = self.client.get(reverse('spirit:comment:publish', kwargs={'topic_id': self.topic.pk, }))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['topic'], self.topic)

    def test_comment_publish_comment_posted(self):
        """
        Should call comment_posted
        """
        res = []

        def mocked_comment_posted(comment, mentions):
            res.append(comment)
            res.append(mentions)

        org_comment_posted, views.comment_posted = views.comment_posted, mocked_comment_posted
        try:
            utils.login(self)
            form_data = {'comment': 'foobar', }
            self.client.post(reverse('spirit:comment:publish', kwargs={'topic_id': self.topic.pk, }),
                             form_data)
            self.assertEqual(len(Comment.objects.all()), 1)
            self.assertEqual(res[0], Comment.objects.first())
            self.assertEqual(res[1], {})
        finally:
            views.comment_posted = org_comment_posted

    def test_comment_publish_on_private(self):
        """
        create comment on private topic
        """
        private = utils.create_private_topic(user=self.user)

        utils.login(self)
        form_data = {'comment': 'foobar', }
        response = self.client.post(reverse('spirit:comment:publish', kwargs={'topic_id': private.topic.pk, }),
                                    form_data)
        expected_url = reverse('spirit:comment:find', kwargs={'pk': 1, })
        self.assertRedirects(response, expected_url, status_code=302, target_status_code=302)
        self.assertEqual(len(Comment.objects.all()), 1)

    def test_comment_publish_on_closed_topic(self):
        """
        should not be able to create a comment on a closed topic
        """
        Topic.objects.filter(pk=self.topic.pk).update(is_closed=True)

        utils.login(self)
        form_data = {'comment': 'foobar', }
        response = self.client.post(reverse('spirit:comment:publish', kwargs={'topic_id': self.topic.pk, }),
                                    form_data)
        self.assertEqual(response.status_code, 404)

    def test_comment_publish_on_closed_cateory(self):
        """
        should be able to create a comment on a closed category (if topic is not closed)
        """
        Category.objects.filter(pk=self.category.pk).update(is_closed=True)

        utils.login(self)
        form_data = {'comment': 'foobar', }
        response = self.client.post(reverse('spirit:comment:publish', kwargs={'topic_id': self.topic.pk, }),
                                    form_data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(Comment.objects.all()), 1)

    def test_comment_publish_on_removed_topic_or_category(self):
        """
        should not be able to create a comment
        """
        # removed category
        Category.objects.all().update(is_removed=True)

        utils.login(self)
        form_data = {'comment': 'foobar', }
        response = self.client.post(reverse('spirit:comment:publish', kwargs={'topic_id': self.topic.pk, }),
                                    form_data)
        self.assertEqual(response.status_code, 404)

        # removed subcategory
        Category.objects.all().update(is_removed=False)
        subcategory = utils.create_category(parent=self.category, is_removed=True)
        topic2 = utils.create_topic(subcategory)

        utils.login(self)
        form_data = {'comment': 'foobar', }
        response = self.client.post(reverse('spirit:comment:publish', kwargs={'topic_id': topic2.pk, }),
                                    form_data)
        self.assertEqual(response.status_code, 404)

        # removed topic
        Category.objects.all().update(is_removed=False)
        Topic.objects.all().update(is_removed=True)

        utils.login(self)
        form_data = {'comment': 'foobar', }
        response = self.client.post(reverse('spirit:comment:publish', kwargs={'topic_id': self.topic.pk, }),
                                    form_data)
        self.assertEqual(response.status_code, 404)

    def test_comment_publish_no_access(self):
        """
        should not be able to create a comment on a private topic if has no access
        """
        private = utils.create_private_topic(user=self.user)
        private.delete()

        utils.login(self)
        form_data = {'comment': 'foobar', }
        response = self.client.post(reverse('spirit:comment:publish', kwargs={'topic_id': private.topic.pk, }),
                                    form_data)
        self.assertEqual(response.status_code, 404)

    def test_comment_publish_quote(self):
        """
        create comment quote
        """
        utils.login(self)
        comment = utils.create_comment(topic=self.topic)
        response = self.client.get(reverse('spirit:comment:publish', kwargs={'topic_id': self.topic.pk,
                                                                             'pk': comment.pk}))
        self.assertEqual(response.context['form'].initial['comment'],
                         markdown.quotify(comment.comment, comment.user.username))

    def test_comment_publish_next(self):
        """
        next on create comment
        """
        utils.login(self)
        form_data = {'comment': 'foobar', 'next': '/fakepath/'}
        response = self.client.post(reverse('spirit:comment:publish', kwargs={'topic_id': self.topic.pk, }),
                                    form_data)
        self.assertRedirects(response, '/fakepath/', status_code=302, target_status_code=404)

    def test_comment_update(self):
        """
        update comment
        """
        comment = utils.create_comment(user=self.user, topic=self.topic)

        utils.login(self)
        form_data = {'comment': 'barfoo', }
        response = self.client.post(reverse('spirit:comment:update', kwargs={'pk': comment.pk, }),
                                    form_data)
        expected_url = reverse('spirit:comment:find', kwargs={'pk': 1, })
        self.assertRedirects(response, expected_url, status_code=302, target_status_code=302)
        self.assertEqual(Comment.objects.get(pk=comment.pk).comment, 'barfoo')

        # next
        form_data.update({'next': '/fakepath/', })
        response = self.client.post(reverse('spirit:comment:update', kwargs={'pk': comment.pk, }),
                                    form_data)
        self.assertRedirects(response, '/fakepath/', status_code=302, target_status_code=404)

    def test_comment_update_not_moderator(self):
        """
        non moderators can not update other people comments
        """
        user = utils.create_user()
        comment = utils.create_comment(user=user, topic=self.topic)

        utils.login(self)
        form_data = {'comment': 'barfoo', }
        response = self.client.post(reverse('spirit:comment:update', kwargs={'pk': comment.pk, }),
                                    form_data)
        self.assertEqual(response.status_code, 404)

    def test_comment_update_moderator(self):
        """
        moderators can update other people comments
        """
        UserProfile.objects.filter(user__pk=self.user.pk).update(is_moderator=True)
        user = utils.create_user()
        comment = utils.create_comment(user=user, topic=self.topic)

        utils.login(self)
        form_data = {'comment': 'barfoo', }
        response = self.client.post(reverse('spirit:comment:update', kwargs={'pk': comment.pk, }),
                                    form_data)
        expected_url = reverse('spirit:comment:find', kwargs={'pk': 1, })
        self.assertRedirects(response, expected_url, status_code=302, target_status_code=302)
        self.assertEqual(Comment.objects.get(pk=comment.pk).comment, 'barfoo')

    def test_comment_update_moderator_private(self):
        """
        moderators can not update comments in private topics they has no access
        """
        UserProfile.objects.filter(user__pk=self.user.pk).update(is_moderator=True)
        user = utils.create_user()
        topic_private = utils.create_private_topic()
        comment = utils.create_comment(user=user, topic=topic_private.topic)

        utils.login(self)
        form_data = {'comment': 'barfoo', }
        response = self.client.post(reverse('spirit:comment:update', kwargs={'pk': comment.pk, }),
                                    form_data)
        self.assertEqual(response.status_code, 404)

    def test_comment_update_increase_modified_count(self):
        """
        Should increase the modified count after an update
        """
        utils.login(self)
        comment_posted = utils.create_comment(user=self.user, topic=self.topic)
        form_data = {'comment': 'my comment, oh!', }
        self.client.post(reverse('spirit:comment:update', kwargs={'pk': comment_posted.pk, }),
                         form_data)
        self.assertEqual(Comment.objects.get(pk=comment_posted.pk).modified_count, 1)

    def test_comment_update_history(self):
        """
        Should add the *first* and *modified* comments to the history
        """
        utils.login(self)
        comment_posted = utils.create_comment(user=self.user, topic=self.topic)
        form_data = {'comment': 'my comment, oh!', }
        self.client.post(reverse('spirit:comment:update', kwargs={'pk': comment_posted.pk, }),
                         form_data)
        comments_history = CommentHistory.objects.filter(comment_fk=comment_posted).order_by('pk')
        self.assertEqual(len(comments_history), 2)  # first and edited
        self.assertIn(comment_posted.comment_html, comments_history[0].comment_html)  # first
        self.assertIn('my comment, oh!', comments_history[1].comment_html)  # modified

    def test_comment_delete_permission_denied_to_non_moderator(self):
        req = RequestFactory().get('/')
        req.user = self.user
        req.user.st.is_moderator = False
        self.assertRaises(PermissionDenied, comment_delete, req)

    def test_comment_delete(self):
        """
        comment delete
        """
        self.user = utils.create_user()
        self.user.st.is_moderator = True
        self.user.st.save()
        comment = utils.create_comment(user=self.user, topic=self.topic)

        utils.login(self)
        form_data = {}
        response = self.client.post(reverse('spirit:comment:delete', kwargs={'pk': comment.pk, }),
                                    form_data)
        expected_url = comment.get_absolute_url()
        self.assertRedirects(response, expected_url, status_code=302, target_status_code=302)

        response = self.client.get(reverse('spirit:comment:delete', kwargs={'pk': comment.pk, }))
        self.assertEqual(response.status_code, 200)

    def test_comment_undelete(self):
        """
        comment undelete
        """
        self.user = utils.create_user()
        self.user.st.is_moderator = True
        self.user.st.save()
        comment = utils.create_comment(user=self.user, topic=self.topic, is_removed=True)

        utils.login(self)
        form_data = {}
        response = self.client.post(reverse('spirit:comment:undelete', kwargs={'pk': comment.pk, }),
                                    form_data)
        expected_url = comment.get_absolute_url()
        self.assertRedirects(response, expected_url, status_code=302, target_status_code=302)

        response = self.client.get(reverse('spirit:comment:undelete', kwargs={'pk': comment.pk, }))
        self.assertEqual(response.status_code, 200)

    def test_comment_move(self):
        """
        comment move to another topic
        """
        utils.login(self)
        self.user.st.is_moderator = True
        self.user.save()
        Topic.objects.filter(pk=self.topic.pk).update(comment_count=2)
        comment = utils.create_comment(user=self.user, topic=self.topic)
        comment2 = utils.create_comment(user=self.user, topic=self.topic)
        to_topic = utils.create_topic(category=self.category)
        form_data = {'topic': to_topic.pk,
                     'comments': [comment.pk, comment2.pk], }
        response = self.client.post(reverse('spirit:comment:move', kwargs={'topic_id': self.topic.pk, }),
                                    form_data)
        expected_url = self.topic.get_absolute_url()
        self.assertRedirects(response, expected_url, status_code=302)
        self.assertEqual(Comment.objects.filter(topic=to_topic.pk).count(), 2)
        self.assertEqual(Comment.objects.filter(topic=self.topic.pk).count(), 0)
        self.assertEqual(Topic.objects.get(pk=self.topic.pk).comment_count, 0)

    def test_comment_find(self):
        """
        comment absolute and lazy url
        """
        comment = utils.create_comment(user=self.user, topic=self.topic)
        response = self.client.post(reverse('spirit:comment:find', kwargs={'pk': comment.pk, }))
        expected_url = comment.topic.get_absolute_url() + "#c%d" % comment.pk
        self.assertRedirects(response, expected_url, status_code=302)

    def test_comment_image_upload(self):
        """
        comment image upload
        """
        utils.login(self)
        img = BytesIO(b'GIF87a\x01\x00\x01\x00\x80\x01\x00\x00\x00\x00ccc,\x00'
                      b'\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')
        files = {'image': SimpleUploadedFile('image.gif', img.read(), content_type='image/gif'), }
        response = self.client.post(reverse('spirit:comment:image-upload-ajax'),
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest',
                                    data=files)
        res = json.loads(response.content.decode('utf-8'))
        image_url = os.path.join(
            settings.MEDIA_URL, 'spirit', 'images', str(self.user.pk),  "bf21c3043d749d5598366c26e7e4ab44.gif"
        ).replace("\\", "/")
        self.assertEqual(res['url'], image_url)
        image_path = os.path.join(
            settings.MEDIA_ROOT, 'spirit', 'images', str(self.user.pk), "bf21c3043d749d5598366c26e7e4ab44.gif"
        )
        self.assertTrue(os.path.isfile(image_path))
        shutil.rmtree(settings.MEDIA_ROOT)  # cleanup

    def test_comment_image_upload_invalid(self):
        """
        comment image upload, invalid image
        """
        utils.login(self)
        image = BytesIO(b'BAD\x02D\x01\x00;')
        image.name = 'image.gif'
        image.content_type = 'image/gif'
        files = {'image': SimpleUploadedFile(image.name, image.read()), }
        response = self.client.post(reverse('spirit:comment:image-upload-ajax'),
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest',
                                    data=files)
        res = json.loads(response.content.decode('utf-8'))
        self.assertIn('error', res.keys())
        self.assertIn('image', res['error'].keys())


class CommentModelsTest(TestCase):

    def setUp(self):
        cache.clear()
        self.user = utils.create_user()
        self.category = utils.create_category()
        self.topic = utils.create_topic(category=self.category, user=self.user)

    def test_comment_increase_modified_count(self):
        """
        Increase modified_count
        """
        comment = utils.create_comment(topic=self.topic)
        comment.increase_modified_count()
        self.assertEqual(Comment.objects.get(pk=comment.pk).modified_count, 1)

    def test_comment_increase_likes_count(self):
        """
        Increase like_count on comment like
        """
        comment = utils.create_comment(topic=self.topic)
        comment.increase_likes_count()
        self.assertEqual(Comment.objects.get(pk=comment.pk).likes_count, 1)

    def test_comment_decrease_likes_count(self):
        """
        Decrease like_count on remove comment like
        """
        comment = utils.create_comment(topic=self.topic, likes_count=1)
        comment.decrease_likes_count()
        self.assertEqual(Comment.objects.get(pk=comment.pk).likes_count, 0)

    def test_comment_create_moderation_action(self):
        """
        Create comment that tells what moderation action was made
        """
        Comment.create_moderation_action(user=self.user, topic=self.topic, action=1)
        self.assertEqual(Comment.objects.filter(user=self.user, topic=self.topic, action=1).count(), 1)


class CommentTemplateTagTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = utils.create_user()
        self.category = utils.create_category()
        self.topic = utils.create_topic(category=self.category, user=self.user)
        utils.create_comment(topic=self.topic)
        utils.create_comment(topic=self.topic)
        utils.create_comment(topic=self.topic)

    def test_render_comments_form(self):
        """
        should display simple comment form
        """
        Template(
            "{% load spirit_tags %}"
            "{% render_comments_form topic %}"
        ).render(Context({'topic': self.topic, }))
        context = render_comments_form(self.topic)
        self.assertEqual(context['next'], None)
        self.assertIsInstance(context['form'], CommentForm)
        self.assertEqual(context['topic_id'], self.topic.pk)

    def test_get_action_text(self):
        """
        should display action
        """
        out = Template(
            "{% load spirit_tags %}"
            "{% get_comment_action_text 1 %}"
        ).render(Context())
        self.assertNotEqual(out, "")


class CommentFormTest(TestCase):

    def setUp(self):
        cache.clear()
        self.user = utils.create_user()
        self.category = utils.create_category()
        self.topic = utils.create_topic(category=self.category)

    def test_comment_create(self):
        form_data = {'comment': 'foo', }
        form = CommentForm(data=form_data)
        self.assertEqual(form.is_valid(), True)

    def test_comment_markdown(self):
        form_data = {'comment': '**Spirit unicode: áéíóú** '
                                '<script>alert();</script>', }
        form = CommentForm(data=form_data)
        self.assertEqual(form.is_valid(), True)
        form.user = self.user
        form.topic = self.topic
        comment = form.save()
        self.assertEqual(comment.comment_html, '<p><strong>Spirit unicode: áéíóú</strong> '
                                               '&lt;script&gt;alert();&lt;/script&gt;</p>')

    def test_comments_move(self):
        comment = utils.create_comment(user=self.user, topic=self.topic)
        comment2 = utils.create_comment(user=self.user, topic=self.topic)
        to_topic = utils.create_topic(category=self.category)
        form_data = {'topic': to_topic.pk,
                     'comments': [comment.pk, comment2.pk], }
        form = CommentMoveForm(topic=self.topic, data=form_data)
        self.assertEqual(form.is_valid(), True)
        self.assertEqual(form.save(), list(Comment.objects.filter(topic=to_topic)))

    def test_comment_image_upload(self):
        """
        Image upload
        """
        content = b'GIF87a\x01\x00\x01\x00\x80\x01\x00\x00\x00\x00ccc,\x00' \
                  b'\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
        img = BytesIO(content)
        files = {'image': SimpleUploadedFile('image.gif', img.read(), content_type='image/gif'), }

        form = CommentImageForm(user=self.user, data={}, files=files)
        self.assertTrue(form.is_valid())
        image = form.save()
        self.assertEqual(image.name, "bf21c3043d749d5598366c26e7e4ab44.gif")
        image_url = os.path.join(settings.MEDIA_URL, 'spirit', 'images', str(self.user.pk),
                                 image.name).replace("\\", "/")
        self.assertEqual(image.url, image_url)
        image_path = os.path.join(settings.MEDIA_ROOT, 'spirit', 'images', str(self.user.pk), image.name)
        self.assertTrue(os.path.isfile(image_path))

        with open(image_path, "rb") as fh:
            self.assertEqual(fh.read(), content)

        os.remove(image_path)

    def test_comment_image_upload_no_extension(self):
        """
        Image upload no extension
        """
        img = BytesIO(b'GIF87a\x01\x00\x01\x00\x80\x01\x00\x00\x00\x00ccc,\x00'
                      b'\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')
        files = {'image': SimpleUploadedFile('image', img.read(), content_type='image/gif'), }
        form = CommentImageForm(user=self.user, data={}, files=files)
        self.assertTrue(form.is_valid())
        image = form.save()
        self.assertEqual(image.name, "bf21c3043d749d5598366c26e7e4ab44.gif")
        os.remove(os.path.join(settings.MEDIA_ROOT, 'spirit', 'images', str(self.user.pk), image.name))

    @override_settings(ST_ALLOWED_UPLOAD_IMAGE_FORMAT=['png', ])
    def test_comment_image_upload_not_allowed_format(self):
        """
        Image upload, invalid format
        """
        img = BytesIO(b'GIF87a\x01\x00\x01\x00\x80\x01\x00\x00\x00\x00ccc,\x00'
                      b'\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')
        # fake png extension
        files = {'image': SimpleUploadedFile('image.png', img.read(), content_type='image/png'), }
        form = CommentImageForm(data={}, files=files)
        self.assertFalse(form.is_valid())

    def test_comment_image_upload_invalid(self):
        """
        Image upload, bad image
        """
        img = BytesIO(b'bad\x00;')
        files = {'image': SimpleUploadedFile('image.gif', img.read(), content_type='image/gif'), }
        form = CommentImageForm(data={}, files=files)
        self.assertFalse(form.is_valid())


class CommentUtilsTest(TestCase):

    def setUp(self):
        cache.clear()
        self.user = utils.create_user()
        self.category = utils.create_category()
        self.topic = utils.create_topic(category=self.category, user=self.user)

    def test_comment_posted(self):
        """
        * Should create subscription
        * Should notify subscribers
        * Should notify mentions
        * Should increase topic's comment counter
        * Should mark the topic as unread
        """
        # Should create subscription
        subscriber = self.user
        comment = utils.create_comment(user=subscriber, topic=self.topic)
        comment_posted(comment=comment, mentions=None)
        self.assertEqual(len(TopicNotification.objects.all()), 1)
        self.assertTrue(TopicNotification.objects.get(user=subscriber, topic=self.topic).is_read)

        # Should notify subscribers
        user = utils.create_user()
        comment = utils.create_comment(user=user, topic=self.topic)
        comment_posted(comment=comment, mentions=None)
        self.assertEqual(len(TopicNotification.objects.all()), 2)
        self.assertFalse(TopicNotification.objects.get(user=subscriber, topic=self.topic).is_read)

        # Should notify mentions
        mentioned = utils.create_user()
        mentions = {mentioned.username: mentioned, }
        comment = utils.create_comment(user=user, topic=self.topic)
        comment_posted(comment=comment, mentions=mentions)
        self.assertEqual(TopicNotification.objects.get(user=mentioned, comment=comment).action, MENTION)
        self.assertFalse(TopicNotification.objects.get(user=mentioned, comment=comment).is_read)

        # Should mark the topic as unread
        user_unread = utils.create_user()
        topic = utils.create_topic(self.category)
        topic_unread_creator = TopicUnread.objects.create(user=user, topic=topic, is_read=True)
        topic_unread_subscriber = TopicUnread.objects.create(user=user_unread, topic=topic, is_read=True)
        comment = utils.create_comment(user=user, topic=topic)
        comment_posted(comment=comment, mentions=None)
        self.assertTrue(TopicUnread.objects.get(pk=topic_unread_creator.pk).is_read)
        self.assertFalse(TopicUnread.objects.get(pk=topic_unread_subscriber.pk).is_read)

        # Should increase topic's comment counter
        topic = utils.create_topic(self.category)
        comment = utils.create_comment(user=user, topic=topic)
        comment_posted(comment=comment, mentions=None)
        self.assertEqual(Topic.objects.get(pk=topic.pk).comment_count, 1)
        comment_posted(comment=comment, mentions=None)
        self.assertEqual(Topic.objects.get(pk=topic.pk).comment_count, 2)

    def test_pre_comment_update(self):
        """
        * Should render static polls
        * Should create comment history maybe
        """
        # Should render static polls
        comment = utils.create_comment(user=self.user, topic=self.topic, comment_html='<poll name=foo>')
        CommentPoll.objects.create(comment=comment, name='foo', title="my poll")
        pre_comment_update(comment=comment)
        self.assertTrue('my poll' in comment.comment_html)

        # Should create comment history maybe
        comment = utils.create_comment(user=self.user, topic=self.topic)
        pre_comment_update(comment=comment)
        self.assertEqual(len(CommentHistory.objects.filter(comment_fk=comment)), 1)
        pre_comment_update(comment=comment)
        self.assertEqual(len(CommentHistory.objects.filter(comment_fk=comment)), 1)

    def test_post_comment_update(self):
        """
        * Should increase modified count
        * Should render static polls
        * Should create comment history
        """
        # Should increase modified count
        comment = utils.create_comment(user=self.user, topic=self.topic)
        post_comment_update(comment=comment)
        self.assertEqual(Comment.objects.get(pk=comment.pk).modified_count, 1)
        post_comment_update(comment=comment)
        self.assertEqual(Comment.objects.get(pk=comment.pk).modified_count, 2)

        # Should render static polls
        comment = utils.create_comment(user=self.user, topic=self.topic, comment_html='<poll name=foo>')
        CommentPoll.objects.create(comment=comment, name='foo', title="my poll")
        post_comment_update(comment=comment)
        self.assertTrue('my poll' in comment.comment_html)

        # Should create comment history
        comment = utils.create_comment(user=self.user, topic=self.topic)
        post_comment_update(comment=comment)
        self.assertEqual(len(CommentHistory.objects.filter(comment_fk=comment)), 1)
        post_comment_update(comment=comment)
        self.assertEqual(len(CommentHistory.objects.filter(comment_fk=comment)), 2)
